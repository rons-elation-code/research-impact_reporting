from django.http import HttpResponse


class HtmxLoginRedirectMiddleware:
    """Return HX-Redirect header instead of 302 for expired HTMX requests.

    Without this, HTMX follows the 302 silently and swaps the login page
    HTML into whatever partial element triggered the poll.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        is_htmx = request.headers.get("HX-Request") == "true"
        is_redirect_to_login = (
            response.status_code in (301, 302)
            and "/login/" in response.get("Location", "")
        )
        if is_htmx and is_redirect_to_login:
            resp = HttpResponse(status=204)
            resp["HX-Redirect"] = response["Location"]
            return resp
        return response
