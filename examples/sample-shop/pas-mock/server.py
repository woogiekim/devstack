from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import json


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.write_json({"status": "UP"})
            return

        if parsed.path == "/v1_0/service/product/products/type/code.json":
            query = parse_qs(parsed.query)
            product_codes = query.get("productCodes", [""])[0]
            result = []
            for raw_code in [code for code in product_codes.split(",") if code]:
                code = int(raw_code)
                result.append(
                    {
                        "productCode": code,
                        "productName": f"테스트 상품 {code}",
                        "imageUrl": f"https://img.example.com/prod_img/500000/{code % 100:02d}/{code}.jpg",
                    }
                )
            self.write_json({"status": 200, "message": "success", "result": result})
            return

        self.send_response(404)
        self.end_headers()

    def write_json(self, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18083), Handler).serve_forever()
