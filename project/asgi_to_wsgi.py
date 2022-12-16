from io import StringIO
import asyncio
from typing import Iterable, Tuple, Dict, Any
from enum import Enum

# ASGI-to-WSGI Wrapper
#
# This wrapper is based on code from two open-source projects.
# 1. The management of the application cycle for the ASGI http connection and
# the use of instance methods as ASGI send/receive functions comes from
# Mangum by Jordan Eremieff.
# 2. The mapping between WSGI environ and ASGI scope and the wrapper interface comes from
# asgiref by the Django Software Foundation and individual contributors.
#
# The copyright notices and permission notices for these appear below.
#
# ----------------------------------------------------------------------------------
#
# MIT License
#
# Copyright (c) 2020 Jordan Eremieff
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# ----------------------------------------------------------------------------------
#
# Copyright (c) Django Software Foundation and individual contributors.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright notice,
#        this list of conditions and the following disclaimer.
#
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#
#     3. Neither the name of Django nor the names of its contributors may be used
#        to endorse or promote products derived from this software without
#        specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


__version__ = '0.0.0'


class HttpState(Enum):
    REQUEST = 1
    RESPONSE = 2
    COMPLETE = 3


class AsgiToWsgi:
    """
    Wraps an HTTP ASGI application to make it into a WSGI application.
    """

    def __init__(self, asgi_application):
        self.asgi_application = asgi_application

    def __call__(self, environ, response) -> Iterable[bytes]:
        return AsgiToWsgiInstance(self.asgi_application)(environ, response)


class AsgiToWsgiInstance:
    def __init__(self, asgi_application):
        self.asgi_application = asgi_application
        self.body = bytearray()

    def __call__(self, environ, response) -> Iterable[bytes]:
        if not environ["SERVER_PROTOCOL"].startswith("HTTP"):
            raise ValueError("ASGI wrapper received a non-HTTP environ")
        self.environ = environ
        self.response = response
        scope, body = self.build_scope_and_body(self.environ)
        asyncio.run(self.run_asgi_app(scope, body))
        return (bytes(self.body),)

    def build_scope_and_body(self, environ) -> Tuple[Dict[str, Any], bytes]:
        headers = []
        for name, value in environ.items():
            # See https://www.python.org/dev/peps/pep-0333/#environ-variables
            if name.startswith("HTTP_"):
                corrected_name = str(name[5:]).lower().replace("_", "-")
            elif name == "CONTENT_LENGTH":
                corrected_name = "content-length"
            elif name == "CONTENT_TYPE":
                corrected_name = "content-type"
            else:
                continue
            headers.append((corrected_name.encode('utf-8'), value.encode('utf-8')))
        scope = {
            "type": "http",
            "method": environ["REQUEST_METHOD"],
            "root_path": environ["SCRIPT_NAME"],
            "path": environ["PATH_INFO"],
            "query_string": environ["QUERY_STRING"],
            "http_version": environ["SERVER_PROTOCOL"][5:],
            "scheme": environ.get("wsgi.url_scheme", "http"),
            "server": (environ["SERVER_NAME"], environ["SERVER_PORT"]),
            "client": (environ["REMOTE_ADDR"],),
            "headers": headers,
        }
        wsgi_input = environ['wsgi.input']
        return scope, wsgi_input.read() if wsgi_input is not None else ''

    async def run_asgi_app(self, scope, body: bytes):
        self.state = HttpState.REQUEST
        self.app_queue = asyncio.Queue()
        self.app_queue.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        await self.asgi_application(scope, self.receive, self.send)

    async def receive(self):
        return await self.app_queue.get()

    async def send(self, message) -> None:
        message_type = message["type"]

        if self.state == HttpState.REQUEST and message_type == "http.response.start":
            status = str(message["status"])
            headers = message.get("headers", [])
            corrected_headers = []
            for (k, v) in headers:
                corrected_headers.append((k.decode('utf-8'), v.decode('utf-8')))
            self.response(status, corrected_headers, None)
            self.state = HttpState.RESPONSE
        elif self.state == HttpState.RESPONSE and message_type == "http.response.body":
            self.body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                self.state = HttpState.COMPLETE
                await self.app_queue.put({"type": "http.disconnect"})
        else:
            raise TypeError(
                f"{self.state}: Unexpected '{message_type}' event received."
            )
