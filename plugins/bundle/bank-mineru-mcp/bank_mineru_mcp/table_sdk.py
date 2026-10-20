"""Controlled Python client for the same fixed table-fact operations.

The trusted parent performs every query and records evidence. Python may
derive its own result; that result cannot manufacture trusted scan evidence.
"""
import json
import sys


class Tables:
    def __init__(self, channel):
        self.channel = channel

    def _request(self, name, arguments):
        self.channel.write(json.dumps({"request": name, "arguments": arguments}, ensure_ascii=False) + "\n")
        self.channel.flush()
        response = json.loads(sys.stdin.readline())
        if "error_code" in response:
            raise ValueError(response["error_code"])
        return response["result"]

    def aggregate(self, ops):
        return self._request("aggregate", {"ops": ops})

    def read_range(self, *, sheet=None, rows=None, row_cursor=None, columns=None, format="records"):
        return self._request("read_range", {"sheet": sheet, "rows": rows, "row_cursor": row_cursor, "columns": columns, "format": format})

    def inventory(self):
        return self._request("inventory", {})


class BoundedText:
    def __init__(self):
        self.size = 0
    def write(self, text):
        self.size += len(text.encode("utf-8"))
        if self.size > 32000:
            raise ValueError("DOCUMENT_RESULT_TOO_LARGE")
        return len(text)
    def flush(self):
        pass


def main():
    request = json.loads(sys.stdin.readline())
    channel = sys.stdout
    sys.stdout = BoundedText()
    sys.stderr = BoundedText()
    namespace = {"tables": Tables(channel), "result": None, "__name__": "__controlled_analysis__"}
    try:
        exec(compile(request["code"], "<controlled-analysis>", "exec"), namespace)
        message = json.dumps({"result": namespace["result"]}, ensure_ascii=False)
        if len(message.encode()) > 32000:
            raise ValueError("Output quota exceeded")
        channel.write(message + "\n")
    except BaseException:
        channel.write('{"error_code":"DOCUMENT_ANALYSIS_FAILED"}\n')
    channel.flush()


if __name__ == "__main__":
    main()
