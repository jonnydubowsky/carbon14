import json

from functools import partial

from django import forms
from django.db.models import QuerySet
from django.db.models.fields.files import FieldFile
from django.http import HttpResponse, JsonResponse
from django.views.generic import View
from django.template import Template, RequestContext
from django.core.serializers.json import DjangoJSONEncoder

from .graphql import parse
from .errors import Carbon14Error
from .neonode import RootNode, Node


class ModelNode(Node):

    class Meta(Node.Meta):
        is_public = False
        optimize = {}

    def query(self, results, kwargs, fields, source=None):
        if source is None:
            source = self.query_optimization(self.Meta.source, fields)
        return super().query(results, kwargs, fields, source=source)

    def query_optimization(self, source, fields, prefix=''):
        for field_name, data in fields.items():
            if field_name in self.Meta.nested_fields:
                source = source.prefetch_related(field_name)

            for f in self.Meta.optimize.get(field_name, []):
                source = source.prefetch_related(prefix + f)

            node = self.get_node_for(field_name)
            if node:
                source = node.query_optimization(
                    source,
                    data['fields'],
                    prefix=field_name + '__'
                )
        return source

    def filter(self, _source, ids=None, limit=None, offset=None, **kwargs):
        instances = _source
        if not self.Meta.is_public and not self.ctx.user.is_authenticated:
            instances = instances.none()

        if ids is not None:
            instances = instances.filter(id__in=ids)

        if offset:
            instances = instances[offset:]

        if limit:
            instances = instances[:limit]

        return instances

    def resolve(self, item, field_name, kwargs):
        resolver = getattr(self, f'resolve_{field_name}', None)
        if resolver:
            value = partial(resolver, item)
        else:
            value = getattr(item, field_name, None)

        all_values = getattr(value, 'all', None)
        if all_values:
            value = all_values()
        elif callable(value):
            value = value(**kwargs)
        return value

    def is_collection(self, value):
        return isinstance(value, QuerySet) or super().is_collection(value)

    def resolve_id(self, item):
        return str(item.id)


class CarbonJSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, FieldFile):
            return o.url
        return super().default(o)


class GrapQLForm(forms.Form):
    query = forms.CharField(widget=forms.Textarea)


class GraphQLView(View):

    template = Template('''
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8" />
          <meta http-equiv="X-UA-Compatible" content="IE=edge,chrome=1" />
          <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1,
            minimum-scale=1,
            maximum-scale=1"
          >
          <title>Carbon14</title>
        </head>
        <body>

          <form method="post">
            {{ form }}
            {% csrf_token %}
            <input type="submit" value="Query">
          </form>

          <pre>{{ answer|safe }}</pre>

        </body>
        </html>

    ''')
    encoder_class = CarbonJSONEncoder
    nodes = tuple()

    def get(self, request):
        return HttpResponse(content=self.render(form=GrapQLForm()))

    def post(self, request):
        is_graphql = request.META.get('CONTENT_TYPE') == 'application/graphql'

        if is_graphql:
            query = request.body.decode()
        else:
            form = GrapQLForm(data=request.POST)
            form.is_valid()
            query = form.cleaned_data['query']

        root_node = RootNode(self.nodes, ctx=request)
        try:
            data = root_node.query(parse(query))
        except Carbon14Error as e:
            data = {'details': str(e)}
            status = 400
        else:
            status = 200

        if is_graphql:
            return JsonResponse(
                data,
                status=status,
                encoder=self.encoder_class
            )
        else:
            data = json.dumps(
                data,
                cls=self.encoder_class,
                indent=2
            )
            return HttpResponse(
                content=self.render(answer=data, form=form),
                status=status,
            )

    def render(self, **kwargs):
        return (
            self.template.render(RequestContext(self.request, kwargs)).encode()
        )
