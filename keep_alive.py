from threading import Thread
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!"


def run():
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    thread = Thread(target=run)
    thread.daemon = True
    thread.start()
