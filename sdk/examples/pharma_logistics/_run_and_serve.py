"""
Wrapper: runs generate_primer_pdf.py then serves a result page on port 9999.
"""
import sys
import os
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "sdk_langgraph_primer.pdf")
result = {"stdout": "", "stderr": "", "success": False}

# --- Run the script ---
try:
    sys.path.insert(0, os.path.dirname(__file__))
    import io
    from contextlib import redirect_stdout, redirect_stderr

    buf_out = io.StringIO()
    buf_err = io.StringIO()

    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_primer_pdf",
            os.path.join(os.path.dirname(__file__), "generate_primer_pdf.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.build_pdf()

    result["stdout"] = buf_out.getvalue()
    result["stderr"] = buf_err.getvalue()
    result["success"] = os.path.isfile(OUTPUT_PATH)
    print(f"[wrapper] stdout: {result['stdout']!r}", flush=True)
    print(f"[wrapper] stderr: {result['stderr']!r}", flush=True)
    print(f"[wrapper] PDF exists: {result['success']}", flush=True)
except Exception as e:
    result["stderr"] = traceback.format_exc()
    print(f"[wrapper] ERROR: {result['stderr']}", flush=True)

# --- Serve result so preview_logs can capture it ---
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"""
        <html><body>
        <h2>Result</h2>
        <pre>stdout: {result['stdout']}</pre>
        <pre>stderr: {result['stderr']}</pre>
        <p>PDF created: {result['success']}</p>
        <p>PDF path: {OUTPUT_PATH}</p>
        </body></html>
        """.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        pass  # suppress default logging

print("[wrapper] Starting HTTP server on port 9999 ...", flush=True)
HTTPServer(("localhost", 9999), Handler).serve_forever()
