from flask import request

def client_ip():
    """
    Caller's address.

    Nginx reaches uWSGI over the uwsgi protocol (uwsgi_pass), and its
    uwsgi_params sets REMOTE_ADDR to $remote_addr — the real client address.

    Deliberately does NOT consult X-Forwarded-For. If a proxy is ever put
    in front of this API, trust its specific header instead.
    """
    return request.remote_addr or 'unknown'
