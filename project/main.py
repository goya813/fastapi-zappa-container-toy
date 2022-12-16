from fastapi import FastAPI

from asgi_to_wsgi import AsgiToWsgi

app = FastAPI()


@app.get("/")
def hello():
    return {"Hello": "World"}


handler = AsgiToWsgi(app)
