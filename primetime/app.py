from flask import Flask, render_template

# Flask finds templates/ and static/ automatically because they sit next to this file.
app = Flask(__name__)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/notifications")
def notifications():
    return render_template("notification.html")


@app.route("/login")
def login():
    return render_template("login.html")


if __name__ == "__main__":
    # debug=True: auto-reload on Python changes and show errors in the browser.
    app.run(debug=True)
