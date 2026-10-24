"""Azure Functions entry point serving the Flask review app via WSGI.

Hosts the whole Flask app (UI + /api/* endpoints + /static assets) on a
single catch-all HTTP trigger, so the deployed surface is identical to the
local server. Requires host.json routePrefix "" and an account whose
PDF_STORAGE_ACCOUNT (or connection string) env vars are set.
"""

import azure.functions as func

from app import app as flask_app

fn_app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@fn_app.route(route="{*path}")
def handle_request(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    return func.WsgiMiddleware(flask_app.wsgi_app).handle(req, context)
