"""Request validation against docs/openapi.json, so the spec is the only
source of truth for field names, types and ranges."""
import copy
import json
import re

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_URI = 'urn:wisar:openapi'


class Spec:
    def __init__(self, path):
        with open(path) as f:
            self.doc = json.load(f)
        resource = Resource.from_contents(self.doc, default_specification=DRAFT202012)
        self._registry = Registry().with_resource(SPEC_URI, resource)
        self._validators = {}

    def public_doc(self, server_url):
        doc = copy.deepcopy(self.doc)
        doc['servers'] = [{'url': server_url, 'description': 'This server'}] + doc.get('servers', [])
        return doc

    def _validator(self, name):
        if name not in self._validators:
            self._validators[name] = Draft202012Validator(
                {'$ref': f'{SPEC_URI}#/components/schemas/{name}'}, registry=self._registry)
        return self._validators[name]

    def errors(self, name, instance):
        """Return [{'pointer', 'detail'}] for every violation, most specific first."""
        out = []
        for err in sorted(self._validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path)):
            out.extend(self._flatten(err, instance, name))
        seen, unique = set(), []
        for e in out:
            key = (e['pointer'], e['detail'])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return unique

    def _flatten(self, err, instance, name):
        path = list(err.absolute_path)
        # The subject is a oneOf with a discriminator; report against the
        # branch the caller chose instead of jsonschema's "not valid under any".
        if err.validator == 'oneOf' and path == ['subject'] and isinstance(instance.get('subject'), dict):
            kind = instance['subject'].get('kind')
            branch = {'listed': 'ListedSubject', 'custom': 'CustomSubject'}.get(kind)
            if branch is None:
                return [{'pointer': '/subject/kind', 'detail': "must be 'listed' or 'custom'"}]
            return [{'pointer': '/subject' + e['pointer'], 'detail': e['detail']}
                    for e in self.errors(branch, instance['subject'])]
        pointer = ''.join('/' + str(p) for p in path)
        detail = err.message
        if err.validator == 'required':
            m = re.match(r"'([^']+)' is a required property", err.message)
            if m:
                pointer += '/' + m.group(1)
                detail = 'is required'
        elif err.validator == 'additionalProperties':
            detail = err.message.replace(' were unexpected', ' are not allowed').replace(' was unexpected', ' is not allowed')
        return [{'pointer': pointer or '/', 'detail': detail}]
