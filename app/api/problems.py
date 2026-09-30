"""RFC 9457 problem responses."""
from flask import jsonify

PROBLEM_TYPE = 'application/problem+json'


class ApiProblem(Exception):
    def __init__(self, status, title, detail=None, errors=None, headers=None):
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors
        self.headers = headers or {}

    def to_dict(self, instance=None):
        body = {'type': 'about:blank', 'title': self.title, 'status': self.status}
        if self.detail:
            body['detail'] = self.detail
        if instance:
            body['instance'] = instance
        if self.errors:
            body['errors'] = self.errors
        return body


def problem_response(problem, instance=None):
    resp = jsonify(problem.to_dict(instance))
    resp.status_code = problem.status
    resp.mimetype = PROBLEM_TYPE
    for k, v in problem.headers.items():
        resp.headers[k] = str(v)
    return resp


def unprocessable(errors, detail='The request body failed validation.'):
    return ApiProblem(422, 'Unprocessable request', detail, errors=errors)
