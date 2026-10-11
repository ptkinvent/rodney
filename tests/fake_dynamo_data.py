"""A fake DynamoDB client: just the calls DynamoDataStore makes, with real semantics for them
(typed attributes, condition expressions, atomic transactions, pagination, ValidationException)."""

import copy
import re
from decimal import Decimal

from botocore.exceptions import ClientError


class _Err(ClientError):
    code = ""

    def __init__(self, message="", reasons=None):
        response = {"Error": {"Code": self.code, "Message": message}}
        if reasons is not None:
            response["CancellationReasons"] = reasons
        super().__init__(response, "Operation")


class ConditionalCheckFailedException(_Err):
    code = "ConditionalCheckFailedException"


class TransactionCanceledException(_Err):
    code = "TransactionCanceledException"


class ValidationException(_Err):
    code = "ValidationException"


def _num(attr):
    return Decimal(attr["N"]) if attr and "N" in attr else None


def _fmt(d: Decimal) -> dict:
    return {"N": format(d.normalize(), "f")}


class FakeDataDynamo:
    class exceptions:
        ConditionalCheckFailedException = ConditionalCheckFailedException
        TransactionCanceledException = TransactionCanceledException

    PAGE = 3  # tiny pages so pagination bugs show up

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.before_transact = None  # callable(fake): simulates a concurrent writer
        self.transactions = 0

    # -- helpers
    @staticmethod
    def _k(key):
        return key["pk"]["S"], key["sk"]["S"]

    def _item(self, key):
        item = self.items.get(self._k(key))
        return copy.deepcopy(item) if item else None

    # -- condition expressions: attribute_exists/_not_exists, =, <, <=, AND, OR, parentheses
    def _check(self, cond, item, names, values) -> bool:
        if not cond:
            return True
        toks = re.findall(r"<=|<|=|\(|\)|[#:]?[A-Za-z_][A-Za-z0-9_]*", cond)
        pos = [0]
        peek = lambda: toks[pos[0]] if pos[0] < len(toks) else None

        def eat():
            pos[0] += 1
            return toks[pos[0] - 1]

        def operand(t):
            if t.startswith(":"):
                return _num(values[t])
            name = names.get(t, t)
            return _num(item.get(name)) if item and name in item else None

        def atom():
            t = eat()
            if t == "(":
                v = expr()
                eat()
                return v
            if t in ("attribute_exists", "attribute_not_exists"):
                eat()
                name = names.get(eat(), None)
                eat()
                exists = item is not None and name in item
                return exists if t == "attribute_exists" else not exists
            left, op, right = operand(t), eat(), operand(eat())
            if left is None or right is None:
                return False
            return {"=": left == right, "<": left < right, "<=": left <= right}[op]

        def conj():
            v = atom()
            while peek() and peek().upper() == "AND":
                eat()
                v = atom() and v
            return v

        def expr():
            v = conj()
            while peek() and peek().upper() == "OR":
                eat()
                v = conj() or v
            return v

        return expr()

    # -- update expressions: SET a = b [+ c], ...  and  ADD a :v, ...
    def _apply(self, item, expression, names, values):
        item = copy.deepcopy(item)

        def path(p):
            return [names.get(part, part) for part in p.strip().split(".")]

        def read(p):
            parts = path(p)
            if parts[0] not in item:
                return None
            node = item[parts[0]]
            for part in parts[1:]:
                if "M" not in node or part not in node["M"]:
                    return None
                node = node["M"][part]
            return node

        def write(p, value):
            parts, node = path(p), item
            for part in parts[:-1]:
                if part not in node or "M" not in node[part]:
                    raise ValidationException("The document path provided in the update expression is invalid for update")
                node = node[part]["M"]
            node[parts[-1]] = value

        def value_of(operand_text):
            t = operand_text.strip()
            m = re.fullmatch(r"if_not_exists\((.+?),\s*(:\w+)\)", t)
            if m:
                existing = read(m.group(1))
                return existing if existing is not None else values[m.group(2)]
            return values[t] if t.startswith(":") else read(t)

        def add(a, b):
            if a is None or b is None or "N" not in a or "N" not in b:
                raise ValidationException("An operand in the update expression has an incorrect data type")
            return _fmt(Decimal(a["N"]) + Decimal(b["N"]))

        for keyword, body in re.findall(r"\b(SET|ADD)\b\s+(.*?)(?=\s+\b(?:SET|ADD)\b|$)", expression):
            clauses, depth, cur = [], 0, ""
            for ch in body:
                depth += ch == "("
                depth -= ch == ")"
                if ch == "," and depth == 0:
                    clauses.append(cur)
                    cur = ""
                else:
                    cur += ch
            clauses.append(cur)
            for clause in clauses:
                if keyword == "SET":
                    lhs, rhs = clause.split("=", 1)
                    terms = [t for t in re.split(r"\s\+\s", rhs.strip())]
                    value = value_of(terms[0])
                    for term in terms[1:]:
                        value = add(value, value_of(term))
                    write(lhs, value)
                else:
                    p, v = clause.split()
                    current = read(p)
                    write(p, values[v] if current is None else add(current, values[v]))
        return item

    # -- the DynamoDB client API
    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        if not self._check(ConditionExpression, self._item(Item), ExpressionAttributeNames or {}, ExpressionAttributeValues or {}):
            raise ConditionalCheckFailedException()
        self.items[self._k(Item)] = copy.deepcopy(Item)

    def get_item(self, TableName, Key, ConsistentRead=False):
        item = self._item(Key)
        return {"Item": item} if item else {}

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        current = self._item(Key)
        if not self._check(ConditionExpression, current, names, values):
            raise ConditionalCheckFailedException()
        new = self._apply(current or dict(Key), UpdateExpression, names, values)
        self.items[self._k(Key)] = new
        return {"Attributes": copy.deepcopy(new)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(self, TableName, Key):
        self.items.pop(self._k(Key), None)

    def transact_write_items(self, TransactItems):
        self.transactions += 1
        if self.before_transact:
            hook, self.before_transact = self.before_transact, None
            hook(self)
        reasons, ok = [], True
        for t in TransactItems:
            (kind, spec), = t.items()
            key = spec["Item"] if kind == "Put" else spec["Key"]
            passed = self._check(spec.get("ConditionExpression"), self._item(key), spec.get("ExpressionAttributeNames", {}), spec.get("ExpressionAttributeValues", {}))
            reasons.append({"Code": "None" if passed else "ConditionalCheckFailed"})
            ok = ok and passed
        if not ok:
            raise TransactionCanceledException("Transaction cancelled", reasons)
        for t in TransactItems:
            (kind, spec), = t.items()
            if kind == "Put":
                self.items[self._k(spec["Item"])] = copy.deepcopy(spec["Item"])
            elif kind == "Delete":
                self.items.pop(self._k(spec["Key"]), None)
            else:
                cur = self._item(spec["Key"])
                self.items[self._k(spec["Key"])] = self._apply(cur or dict(spec["Key"]), spec["UpdateExpression"], spec.get("ExpressionAttributeNames", {}), spec.get("ExpressionAttributeValues", {}))

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, ConsistentRead=False,
              ProjectionExpression=None, ExclusiveStartKey=None):
        assert KeyConditionExpression == "#pk = :pk"
        pk = ExpressionAttributeValues[":pk"]["S"]
        rows = sorted((k for k in self.items if k[0] == pk), key=lambda k: k[1])
        if ExclusiveStartKey:
            rows = [k for k in rows if k[1] > ExclusiveStartKey["sk"]["S"]]
        page, rest = rows[: self.PAGE], rows[self.PAGE :]
        items = [copy.deepcopy(self.items[k]) for k in page]
        if ProjectionExpression:
            wanted = [ExpressionAttributeNames.get(n.strip(), n.strip()) for n in ProjectionExpression.split(",")]
            items = [{k: v for k, v in i.items() if k in wanted} for i in items]
        res = {"Items": items}
        if rest:
            res["LastEvaluatedKey"] = {"pk": {"S": pk}, "sk": {"S": page[-1][1]}}
        return res

    def batch_write_item(self, RequestItems):
        for requests in RequestItems.values():
            assert len(requests) <= 25
            for r in requests:
                self.items.pop(self._k(r["DeleteRequest"]["Key"]), None)
        return {"UnprocessedItems": {}}
