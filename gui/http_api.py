#!/usr/bin/env python
"""HTTP API logic that ties API call handlers with HTTP routes."""



import json
import urllib2


# pylint: disable=g-bad-import-order,unused-import
from grr.gui import django_lib
# pylint: enable=g-bad-import-order,unused-import

from django import http

from werkzeug import exceptions as werkzeug_exceptions
from werkzeug import routing

import logging

from grr.gui import api_call_handlers
from grr.gui import api_plugins
from grr.gui import http_routing
from grr.lib import access_control
from grr.lib import rdfvalue
from grr.lib import registry
from grr.lib import utils


def BuildToken(request, execution_time):
  """Build an ACLToken from the request."""

  if request.method == "GET":
    reason = request.GET.get("reason", "")
  elif request.method == "POST":
    # The header X-GRR-REASON is set in api-service.js, which django converts to
    # HTTP_X_GRR_REASON.
    reason = utils.SmartUnicode(urllib2.unquote(
        request.META.get("HTTP_X_GRR_REASON", "")))

  token = access_control.ACLToken(
      username=request.user,
      reason=reason,
      process="GRRAdminUI",
      expiry=rdfvalue.RDFDatetime().Now() + execution_time)

  for field in ["REMOTE_ADDR", "HTTP_X_FORWARDED_FOR"]:
    remote_addr = request.META.get(field, "")
    if remote_addr:
      token.source_ips.append(remote_addr)
  return token


def StripTypeInfo(rendered_data):
  """Strips type information from rendered data. Useful for debugging."""

  if isinstance(rendered_data, (list, tuple)):
    return [StripTypeInfo(d) for d in rendered_data]
  elif isinstance(rendered_data, dict):
    if "value" in rendered_data:
      return StripTypeInfo(rendered_data["value"])
    else:
      result = {}
      for k, v in rendered_data.items():
        result[k] = StripTypeInfo(v)
      return result
  else:
    return rendered_data


def RegisterHttpRouteHandler(method, route, handler_cls):
  """Registers given ApiCallHandler for given method and route."""
  http_routing.HTTP_ROUTING_MAP.add(routing.Rule(
      route, methods=[method],
      endpoint=handler_cls))


def GetHandlerForHttpRequest(request):
  """Returns a handler to handle given HTTP request."""

  matcher = http_routing.HTTP_ROUTING_MAP.bind(
      "%s:%s" % (request.environ["SERVER_NAME"],
                 request.environ["SERVER_PORT"]))
  try:
    match = matcher.match(request.path, request.method)
  except werkzeug_exceptions.NotFound:
    raise api_call_handlers.ApiCallHandlerNotFoundError(
        "No API handler was found for (%s) %s" % (request.path,
                                                  request.method))

  handler_cls, route_args = match
  return (handler_cls(), route_args)


def FillAdditionalArgsFromRequest(request, supported_types):
  """Creates arguments objects from a given request dictionary."""

  results = {}
  for key, value in request.items():
    try:
      request_arg_type, request_attr = key.split(".", 1)
    except ValueError:
      continue

    arg_class = None
    for key, supported_type in supported_types.items():
      if key == request_arg_type:
        arg_class = supported_type

    if arg_class:
      if request_arg_type not in results:
        results[request_arg_type] = arg_class()

      results[request_arg_type].Set(request_attr, value)

  results_list = []
  for name, arg_obj in results.items():
    additional_args = api_call_handlers.ApiCallAdditionalArgs(
        name=name, type=supported_types[name].__name__)
    additional_args.args = arg_obj
    results_list.append(additional_args)

  return results_list


class JSONEncoderWithRDFPrimitivesSupport(json.JSONEncoder):
  """Custom JSON encoder that encodes handlers output.

  Custom encoder is required to facilitate usage of primitive values -
  booleans, integers and strings - in handlers responses.

  If handler references an RDFString, RDFInteger or and RDFBOol when building a
  response, it will lead to JSON encoding failure when response encoded,
  unless this custom encoder is used. Another way to solve this issue would be
  to explicitly call api_value_renderers.RenderValue on every value returned
  from the renderer, but it will make the code look overly verbose and dirty.
  """

  def default(self, obj):
    if isinstance(obj, (rdfvalue.RDFInteger,
                        rdfvalue.RDFBool,
                        rdfvalue.RDFString)):
      return obj.SerializeToDataStore()

    return json.JSONEncoder.default(self, obj)


def BuildResponse(status, rendered_data):
  """Builds HTTPResponse object from rendered data and HTTP status."""
  response = http.HttpResponse(status=status,
                               content_type="application/json; charset=utf-8")
  response["Content-Disposition"] = "attachment; filename=response.json"
  response["X-Content-Type-Options"] = "nosniff"

  response.write(")]}'\n")  # XSSI protection

  # To avoid IE content sniffing problems, escape the tags. Otherwise somebody
  # may send a link with malicious payload that will be opened in IE (which
  # does content sniffing and doesn't respect Content-Disposition header) and
  # IE will treat the document as html and executre arbitrary JS that was
  # passed with the payload.
  str_data = json.dumps(rendered_data, cls=JSONEncoderWithRDFPrimitivesSupport)
  response.write(str_data.replace("<", r"\u003c").replace(">", r"\u003e"))

  return response


def RenderHttpResponse(request):
  """Handles given HTTP request with one of the available API handlers."""

  handler, route_args = GetHandlerForHttpRequest(request)

  strip_type_info = False

  if hasattr(request, "GET") and request.GET.get("strip_type_info", ""):
    strip_type_info = True

  if request.method == "GET":
    if handler.args_type:
      unprocessed_request = request.GET
      if hasattr(unprocessed_request, "dict"):
        unprocessed_request = unprocessed_request.dict()

      args = handler.args_type()
      for type_info in args.type_infos:
        if type_info.name in route_args:
          args.Set(type_info.name, route_args[type_info.name])
        elif type_info.name in unprocessed_request:
          args.Set(type_info.name, unprocessed_request[type_info.name])

      if handler.additional_args_types:
        if not hasattr(args, "additional_args"):
          raise RuntimeError("Handler %s defines additional arguments types "
                             "but its arguments object does not have "
                             "'additional_args' field." % handler)

        if hasattr(handler.additional_args_types, "__call__"):
          additional_args_types = handler.additional_args_types()
        else:
          additional_args_types = handler.additional_args_types

        args.additional_args = FillAdditionalArgsFromRequest(
            unprocessed_request, additional_args_types)

    else:
      args = None
  elif request.method == "POST":
    try:
      args = handler.args_type()
      for type_info in args.type_infos:
        if type_info.name in route_args:
          args.Set(type_info.name, route_args[type_info.name])

      if request.META["CONTENT_TYPE"].startswith("multipart/form-data;"):
        payload = json.loads(request.POST["_params_"])
        args.FromDict(payload)

        for name, fd in request.FILES.items():
          args.Set(name, fd.read())
      else:
        payload = json.loads(request.body)
        if payload:
          args.FromDict(payload)
    except Exception as e:  # pylint: disable=broad-except
      logging.exception(
          "Error while parsing POST request %s (%s): %s",
          request.path, request.method, e)

      return BuildResponse(500, dict(message=str(e)))
  else:
    raise RuntimeError("Unsupported method: %s." % request.method)

  token = BuildToken(request, handler.max_execution_time)

  try:
    rendered_data = api_call_handlers.HandleApiCall(handler, args,
                                                    token=token)

    if strip_type_info:
      rendered_data = StripTypeInfo(rendered_data)

    return BuildResponse(200, rendered_data)
  except access_control.UnauthorizedAccess as e:
    logging.exception(
        "Access denied to %s (%s) with %s: %s", request.path,
        request.method, handler.__class__.__name__, e)

    return BuildResponse(403, dict(
        message="Access denied by ACL: %s" % e.message,
        subject=utils.SmartStr(e.subject)))
  except Exception as e:  # pylint: disable=broad-except
    logging.exception(
        "Error while processing %s (%s) with %s: %s", request.path,
        request.method, handler.__class__.__name__, e)

    return BuildResponse(500, dict(message=str(e)))


class HttpApiInitHook(registry.InitHook):
  """Register HTTP API handlers."""

  def RunOnce(self):
    # The list is alphabetized by route.
    RegisterHttpRouteHandler("GET", "/api/aff4/<path:aff4_path>",
                             api_plugins.aff4.ApiGetAff4ObjectHandler)
    RegisterHttpRouteHandler("GET", "/api/aff4-index/<path:aff4_path>",
                             api_plugins.aff4.ApiGetAff4IndexHandler)

    RegisterHttpRouteHandler("GET", "/api/artifacts",
                             api_plugins.artifact.ApiListArtifactsHandler)
    RegisterHttpRouteHandler("POST", "/api/artifacts/upload",
                             api_plugins.artifact.ApiUploadArtifactHandler)
    RegisterHttpRouteHandler("POST", "/api/artifacts/delete",
                             api_plugins.artifact.ApiDeleteArtifactsHandler)

    RegisterHttpRouteHandler("GET", "/api/clients/kb-fields",
                             api_plugins.client.ApiListKbFieldsHandler)
    RegisterHttpRouteHandler("GET", "/api/clients",
                             api_plugins.client.ApiSearchClientsHandler)
    RegisterHttpRouteHandler("GET", "/api/clients/<client_id>",
                             api_plugins.client.ApiGetClientHandler)
    RegisterHttpRouteHandler("GET", "/api/clients/labels",
                             api_plugins.client.ApiListClientsLabelsHandler)
    RegisterHttpRouteHandler("POST", "/api/clients/labels/add",
                             api_plugins.client.ApiAddClientsLabelsHandler)
    RegisterHttpRouteHandler("POST", "/api/clients/labels/remove",
                             api_plugins.client.ApiRemoveClientsLabelsHandler)
    RegisterHttpRouteHandler(
        "POST", "/api/clients/<client_id>/vfs-refresh-operations",
        api_plugins.client.ApiCreateVfsRefreshOperationHandler)

    RegisterHttpRouteHandler("GET", "/api/clients/<client_id>/flows",
                             api_plugins.flow.ApiListClientFlowsHandler)
    RegisterHttpRouteHandler("GET", "/api/clients/<client_id>/flows/<flow_id>",
                             api_plugins.flow.ApiGetFlowHandler)
    RegisterHttpRouteHandler("POST", "/api/clients/<client_id>/flows",
                             api_plugins.flow.ApiCreateFlowHandler)
    RegisterHttpRouteHandler(
        "POST",
        "/api/clients/<client_id>/flows/<flow_id>/actions/cancel",
        api_plugins.flow.ApiCancelFlowHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/clients/<client_id>/flows/<flow_id>/results",
        api_plugins.flow.ApiListFlowResultsHandler)
    RegisterHttpRouteHandler(
        "GET",
        "/api/clients/<client_id>/flows/<flow_id>/results/export-command",
        api_plugins.flow.ApiGetFlowResultsExportCommandHandler)
    RegisterHttpRouteHandler(
        "POST",
        "/api/clients/<client_id>/flows/<flow_id>/results/archive-files",
        api_plugins.flow.ApiArchiveFlowFilesHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/clients/<client_id>/flows/<flow_id>/output-plugins",
        api_plugins.flow.ApiListFlowOutputPluginsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/clients/<client_id>/flows/<flow_id>/"
        "output-plugins/<plugin_id>/logs",
        api_plugins.flow.ApiListFlowOutputPluginLogsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/clients/<client_id>/flows/<flow_id>/"
        "output-plugins/<plugin_id>/errors",
        api_plugins.flow.ApiListFlowOutputPluginErrorsHandler)

    RegisterHttpRouteHandler("GET", "/api/cron-jobs",
                             api_plugins.cron.ApiListCronJobsHandler)
    RegisterHttpRouteHandler("POST", "/api/cron-jobs",
                             api_plugins.cron.ApiCreateCronJobHandler)

    RegisterHttpRouteHandler("GET", "/api/config",
                             api_plugins.config.ApiGetConfigHandler)
    RegisterHttpRouteHandler("GET", "/api/config/<name>",
                             api_plugins.config.ApiGetConfigOptionHandler)

    RegisterHttpRouteHandler("GET", "/api/docs",
                             api_plugins.docs.ApiGetDocsHandler)

    # TODO(user): this handler should be merged with ApiGetFlowHandler after
    # the API-routing and ACLs refactoring.
    RegisterHttpRouteHandler("GET", "/api/flows/<client_id>/<flow_id>/status",
                             api_plugins.flow.ApiGetFlowStatusHandler)
    RegisterHttpRouteHandler("GET", "/api/flows/descriptors",
                             api_plugins.flow.ApiListFlowDescriptorsHandler)
    # TODO(user): an URL for this one doesn't seem entirely correct. Come up
    # with an URL naming scheme that will separate flows with operations that
    # can be triggered remotely without authorization.
    RegisterHttpRouteHandler(
        "POST", "/api/clients/<client_id>/flows/remotegetfile",
        api_plugins.flow.ApiStartGetFileOperationHandler)
    # DEPRECATED: This handler is deprecated as the URL breaks REST conventions.
    RegisterHttpRouteHandler("POST", "/api/clients/<client_id>/flows/start",
                             api_plugins.flow.ApiCreateFlowHandler)
    # This starts global flows.
    RegisterHttpRouteHandler("POST", "/api/flows",
                             api_plugins.flow.ApiCreateFlowHandler)

    RegisterHttpRouteHandler(
        "GET", "/api/output-plugins/all",
        api_plugins.output_plugin.ApiOutputPluginsListHandler)

    RegisterHttpRouteHandler("GET", "/api/hunts",
                             api_plugins.hunt.ApiListHuntsHandler)
    RegisterHttpRouteHandler("GET", "/api/hunts/<hunt_id>",
                             api_plugins.hunt.ApiGetHuntHandler)
    RegisterHttpRouteHandler("GET", "/api/hunts/<hunt_id>/errors",
                             api_plugins.hunt.ApiListHuntErrorsHandler)
    RegisterHttpRouteHandler("GET", "/api/hunts/<hunt_id>/log",
                             api_plugins.hunt.ApiListHuntLogsHandler)
    RegisterHttpRouteHandler("GET", "/api/hunts/<hunt_id>/results",
                             api_plugins.hunt.ApiListHuntResultsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/hunts/<hunt_id>/results/export-command",
        api_plugins.hunt.ApiGetHuntResultsExportCommandHandler)
    RegisterHttpRouteHandler("GET", "/api/hunts/<hunt_id>/output-plugins",
                             api_plugins.hunt.ApiListHuntOutputPluginsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/hunts/<hunt_id>/output-plugins/<plugin_id>/logs",
        api_plugins.hunt.ApiListHuntOutputPluginLogsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/hunts/<hunt_id>/output-plugins/<plugin_id>/errors",
        api_plugins.hunt.ApiListHuntOutputPluginErrorsHandler)
    RegisterHttpRouteHandler("POST", "/api/hunts/create",
                             api_plugins.hunt.ApiCreateHuntHandler)
    RegisterHttpRouteHandler("POST",
                             "/api/hunts/<hunt_id>/results/archive-files",
                             api_plugins.hunt.ApiArchiveHuntFilesHandler)

    RegisterHttpRouteHandler(
        "GET", "/api/reflection/aff4/attributes",
        api_plugins.reflection.ApiListAff4AttributesDescriptorsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/reflection/rdfvalue/<type>",
        api_plugins.reflection.ApiGetRDFValueDescriptorHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/reflection/rdfvalue/all",
        api_plugins.reflection.ApiListRDFValuesDescriptorsHandler)

    RegisterHttpRouteHandler(
        "GET", "/api/stats/store/<component>/metadata",
        api_plugins.stats.ApiListStatsStoreMetricsMetadataHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/stats/store/<component>/metrics/<metric_name>",
        api_plugins.stats.ApiGetStatsStoreMetricHandler)

    RegisterHttpRouteHandler(
        "POST", "/api/users/me/approvals/client/<client_id>",
        api_plugins.user.ApiCreateUserClientApprovalHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/users/me/approvals/client/<client_id>/<reason>",
        api_plugins.user.ApiGetUserClientApprovalHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/users/me/approvals/client",
        api_plugins.user.ApiListUserClientApprovalsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/users/me/approvals/hunt",
        api_plugins.user.ApiListUserHuntApprovalsHandler)
    RegisterHttpRouteHandler(
        "GET", "/api/users/me/approvals/cron",
        api_plugins.user.ApiListUserCronApprovalsHandler)

    RegisterHttpRouteHandler("GET", "/api/users/me/settings",
                             api_plugins.user.ApiGetUserSettingsHandler)
    RegisterHttpRouteHandler("POST", "/api/users/me/settings",
                             api_plugins.user.ApiUpdateUserSettingsHandler)
