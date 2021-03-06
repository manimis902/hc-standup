import asyncio
from functools import wraps
from datetime import datetime, timedelta
import logging
from aiohttp import web
from aiohttp_ac_hipchat.addon_app import create_addon_app
import json
from aiohttp_ac_hipchat.util import http_request
import aiohttp_jinja2
from aiohttp_ac_hipchat.websocket import websocket_setup
import arrow
import bleach
import jinja2
import markdown
import os
import aiolocals

GLANCE_MODULE_KEY = "hcstandup.glance"
USER_CACHE_KEY = "hipchat-user:{group_id}:{user_id}"

log = logging.getLogger(__name__)

SCOPES_V1 = ["view_group", "send_notification"]
SCOPES_V2 = ["view_group", "send_notification", "view_room"]

def get_scopes(context):
    return SCOPES_V2 if context.get("hipchat_server", False) else SCOPES_V1

app, addon = create_addon_app(addon_key="hc-standup",
                              addon_name="HC Standup",
                              vendor_name="Atlassian",
                              vendor_url="https://atlassian.com",
                              from_name="Standup",
                              scopes=get_scopes,
                              avatar="/static/standup.png")

aiohttp_jinja2.setup(app, autoescape=True, loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), 'views')))


def logged(func):
    @asyncio.coroutine
    @wraps(func)
    def inner(*args, **kwargs):
        log.debug("[ENTER] {func_name}".format(func_name=func.__name__))
        result = (yield from func(*args, **kwargs))
        log.debug("[EXIT] {func_name}".format(func_name=func.__name__))
        return result

    return inner


@asyncio.coroutine
def init(app):
    @asyncio.coroutine
    def send_welcome(event):
        client = event['client']
        yield from client.room_client.send_notification(text="HC Standup was added to this room. Type '/standup I did *this*' to get started (yes, "
                                                    "you can use Markdown).")

    app['addon'].register_event('install', send_welcome)
    yield from websocket_setup(app)

app.add_hook("before_first_request", init)

@logged
@asyncio.coroutine
@addon.glance(GLANCE_MODULE_KEY, "Standup",
              addon.relative_to_base("/static/standup.png"), addon.relative_to_base("/static/standup.png"),
              path="/glance",
              target="hcstandup.sidebar")
def get_glance(request):
    spec, statuses = yield from find_statuses(app, request.client)

    return web.Response(text=json.dumps(glance_json(statuses)))

def glance_json(statuses):
    return {
        "label": {
            "type": "html",
            "value": "<strong>%s</strong> Standup reports" % len(statuses.items())
        }
    }

@logged
@asyncio.coroutine
def find_statuses(app, client):
    spec = status_spec(client)
    data = yield from standup_db(app).find_one(spec)
    if not data:
        statuses = {}
    else:
        statuses = data.get('users', {})
        result = {}
        for mention_name, status in statuses.items():
            if status and status['date'].replace(tzinfo=None) > datetime.utcnow()-timedelta(days=3):
                result[mention_name] = status
            else:
                print("Filtering status from %s of date %s" % (mention_name, status.get('date')))

        statuses = result

    return spec, statuses

@logged
@asyncio.coroutine
@addon.webhook("room_message", pattern="^/(?:status|standup)(\s|$).*", path="/standup", auth=None)
def standup_webhook(request):
    addon = request.app['addon']
    body = yield from request.json()
    client_id = body['oauth_client_id']
    client = yield from addon.load_client(client_id)
    if client:
        status = str(body['item']["message"]["message"][len("/standup"):]).strip()
        from_user = body['item']['message']['from']
        room = body['item']['room']

        if not status:
            yield from display_all_statuses(app, client)
        elif status.startswith("@") and ' ' not in status:
            yield from display_one_status(app, client, mention_name=status)
        elif status == "clear":
            yield from clear_status(app, client, from_user, room)
        else:
            yield from record_status(app, client, from_user, status, room, request)
    else:
        log.info("Unknown oauth client id {oauth_client_id}".format(oauth_client_id=client_id))

    return web.Response(status=204)

@asyncio.coroutine
def clear_status(app, client, from_user, room):
    spec, statuses = yield from find_statuses(app, client)

    user_mention = from_user['mention_name']
    if user_mention in statuses:
        del statuses[user_mention]

    data = dict(spec)
    data['users'] = statuses

    yield from standup_db(app).update(spec, data, upsert=True)

    yield from client.room_client.send_notification(text="Status Cleared")
    yield from update_glance(app, client, room)
    yield from send_udpate(client, room["id"], {
        "user_id": from_user["id"],
        "html": ""
    })

@asyncio.coroutine
def display_one_status(app, client, mention_name):
    spec, statuses = yield from find_statuses(app, client)

    if mention_name.startswith("@"):
        mention_name = mention_name[1:]

    status = statuses.get(mention_name)
    if status:
        yield from client.room_client.send_notification(html=render_status(status))
    else:
        yield from client.room_client.send_notification(text="No status found. "
                                                        "Type '/standup I did this' to add your own status.")

@asyncio.coroutine
def display_all_statuses(app, client):
    spec, statuses = yield from find_statuses(app, client)

    if statuses:
        yield from client.room_client.send_notification(html=render_all_statuses(statuses))
    else:
        yield from client.room_client.send_notification(text="No status found. "
                                                        "Type '/standup I did this' to add your own status.")

@asyncio.coroutine
def record_status(app, client, from_user, status, room, request, send_notification=True):
    spec, statuses = yield from find_statuses(app, client)

    user_mention = from_user['mention_name']

    avatar_url = from_user.get('photo_url', None)
    if not avatar_url:
        avatar_url = yield from get_photo_url(client, from_user['id'], room['id'])
        from_user['photo_url'] = avatar_url

    statuses[user_mention] = {
        "user": from_user,
        "message": status,
        "date": datetime.utcnow()
    }

    data = dict(spec)
    data['users'] = statuses

    yield from standup_db(app).update(spec, data, upsert=True)

    if send_notification:
        user_name = from_user.get('name',None)
        if user_name:
            message_text = user_name + " has submitted the standup report. Type '/standup' to see the full report."
        else:
            message_text = "Status recorded. Type '/standup' to see the full report."
        aiolocals.wrap_async(client.room_client.send_notification(text=message_text, card=card_json(from_user, status)))
    aiolocals.wrap_async(update_glance(app, client, room))
    aiolocals.wrap_async(update_sidebar(from_user, request, statuses, user_mention, client, room))

def card_json(user, status):
    user_name = user.get('name', None)
    return {
        "style": "application",
        "format": "medium",
        "id": user_name + str(datetime.utcnow()),
        "title": user_name + "'s standup report.",
        "description": status,
        "activity": {
          "icon": user['photo_url'],
          "html": "<strong>" + user_name + '</strong> has submitted <a href="#" data-target="hc-standup:hcstandup.sidebar">the standup report.</a>'
        }
    }

@logged
@asyncio.coroutine
def update_sidebar(from_user, request, statuses, user_mention, client, room):
    html = aiohttp_jinja2.render_string("_status.jinja2", request, {
        "status": status_to_view(statuses[user_mention])
    }, app_key="aiohttp_jinja2_environment")
    yield from send_udpate(client, room["id"], {
        "user_id": from_user["id"],
        "html": html
    })


@asyncio.coroutine
def get_photo_url(client, user_id, room_id):
    user = (yield from get_user(app, client, room_id, user_id))
    photo_url = user['photo_url'] if user else None
    if not photo_url:
        photo_url = None

    return photo_url

@asyncio.coroutine
def get_room_participants(app, client, room_id_or_name):
    redis_pool = app['redis_pool']
    if not client.has_scope("view_room"):
        return []

    token = yield from client.get_token(redis_pool, scopes=['view_room'])
    with (yield from http_request('GET', "%s/room/%s/participant?expand=items" % (client.api_base_url, room_id_or_name),
                                  headers={'content-type': 'application/json',
                                           'authorization': 'Bearer %s' % token},
                                  timeout=10)) as resp:
        if resp.status == 200:
            body = yield from resp.read(decode=True)
            room_participants = body['items']

            for room_participant in room_participants:
                cache_key = USER_CACHE_KEY.format(group_id=client.group_id, user_id=room_participant['id'])
                subset_room_participant = {k: room_participant.get(k, None) for k in ('id', 'name', 'mention_name',
                                                                                      'photo_url', 'xmpp_jid',
                                                                                      'timezone')}
                yield from redis_pool.setex(key=cache_key, value=json.dumps(subset_room_participant), seconds=3600)

            return room_participants

    return []

@logged
@asyncio.coroutine
def update_glance(app, client, room):
    spec, statuses = yield from find_statuses(app, client)
    yield from push_glance_update(app, client, room['id'], {
        "glance": [{
            "key": GLANCE_MODULE_KEY,
            "content": glance_json(statuses)
        }]
    })

@asyncio.coroutine
def push_glance_update(app, client, room_id_or_name, glance):
    if not client.has_scope("view_room"):
        return

    token = yield from client.get_token(app['redis_pool'], scopes=['view_room'])
    with (yield from http_request('POST', "%s/addon/ui/room/%s" % (client.api_base_url, room_id_or_name),
                                  headers={'content-type': 'application/json',
                                           'authorization': 'Bearer %s' % token},
                                  data=json.dumps(glance),
                                  timeout=10)) as resp:
        if resp.status == 200:
            body = yield from resp.read(decode=True)
            return body['items']

@logged
@asyncio.coroutine
@addon.webpanel("hcstandup.sidebar", "Standup reports", location="hipchat.sidebar.right", path="/report")
@aiohttp_jinja2.template('report.jinja2')
def report_view(request):
    """
        Render the report view
    """
    return {
        "base_url": app["config"]["BASE_URL"],
        "signed_request": request.token,
        "theme": request.theme,
        "room_id": request.jwt_data["context"]["room_id"],
        "create_new_report_enabled": os.environ.get("create_new_report_enabled", False)
    }

@logged
@asyncio.coroutine
@addon.require_jwt()
def get_statuses(request):
    _, statuses = yield from find_statuses(app, request.client)

    web.Response(text=json.dumps(glance_json(statuses)))

@logged
@asyncio.coroutine
@addon.require_jwt()
@aiohttp_jinja2.template('statuses.jinja2')
def get_statuses_view(request):
    results = []
    _, statuses = yield from find_statuses(app, request.client)
    for status in statuses.values():
        results.append(status_to_view(status))

    return {
        "statuses": results
    }

@logged
@asyncio.coroutine
@addon.dialog("hcstandup.dialog", "New report", path="/dialog",
              options={
                  "size": {
                      "width": "600px",
                      "height": "180px"
                  },
                  "primaryAction": {
                      "name": {
                          "value": "Submit",
                      },
                      "key": "dialog.submit",
                      "enabled": True
                  }
              })
@aiohttp_jinja2.template('create.jinja2')
def create_new_report_view(request):
    spec, statuses = yield from find_statuses(app, request.client)

    room_id = request.jwt_data['context']['room_id']
    user_id = request.jwt_data['prn']

    last_status = None
    for status in statuses.values():
        if str(status['user']['id']) == request.jwt_data['prn']:
            last_status = status_to_view(status)
            break
    if not last_status:
        user = yield from get_user(request.app, request.client, room_id, user_id)

        last_status = {
            "date": "Never",
            "user": user,
            "message_html": render_markdown_as_safe_html("**Yesterday I worked on:**   \n" +
                            """ ¯\\\_(ツ)\_/¯""")
        }

    return {
        "base_url": app["config"]["BASE_URL"],
        "status": last_status,
        "signed_request": request.signed_request,
        "report_template": "**Yesterday I worked on:**   \n" +
                           "1.  \n\n" +
                           "**Today**  \n" +
                           "1.  "
    }


@logged
@asyncio.coroutine
@addon.require_jwt()
def create_new_report(request):
    body = yield from request.json()

    status = body['message']
    room_id = request.jwt_data['context']['room_id']
    user_id = request.jwt_data['prn']

    room = {
        "id": room_id
    }
    from_user = yield from get_user(request.app, request.client, room_id, user_id)

    if not from_user:
        return web.Response(status=401)
    else:
        yield from record_status(app, request.client, from_user, status, room, request)
        return web.Response(status=204)

@asyncio.coroutine
def get_user(app, client, room_id, user_id):
    user_key = USER_CACHE_KEY.format(group_id=client.group_id, user_id=user_id)
    cached_data = (yield from app['redis_pool'].get(user_key))
    user = json.loads(cached_data) if cached_data else None

    if not user:
        room_participants = yield from get_room_participants(app, client, room_id)
        for room_participant in room_participants:
            if room_participant['id'] == int(user_id):
                user = room_participant

    return user

@asyncio.coroutine
def send_udpate(client, room_id, data):
    yield from app["websocket"].publish(client.id, room_id, data)

def status_to_view(status):
    msg_date = arrow.get(status['date'])
    message = status['message']
    html = render_markdown_as_safe_html(message)

    return {
        "date": msg_date.humanize(),
        "user": status['user'],
        "message_html": html
    }

def render_all_statuses(statuses):
    txt = ""
    for status in statuses.values():
        txt += render_status(status) + "<br>"
    return txt


def render_status(status):
    msg_date = arrow.get(status['date'])

    message = status['message']
    html = render_markdown_as_safe_html(message)
    name = status['user']['name']
    return "<b>{name}</b>: {message} -- <i>{ago}</i>".format(name=name, message=html, ago=msg_date.humanize())

def status_spec(client):
    return {
        "client_id": client.id,
        "group_id": client.group_id,
        "capabilities_url": client.capabilities_url
    }

allowed_tags = bleach.ALLOWED_TAGS + ["p", "br"]
def render_markdown_as_safe_html(message):
    html = markdown.markdown(message)

    return bleach.clean(html, tags=allowed_tags, strip=True)

def standup_db(app):
    return app['mongodb']['standup']

app.router.add_static('/static', os.path.join(os.path.dirname(__file__), 'static'), name='static')
app.router.add_route('GET', '/status', get_statuses)
app.router.add_route('GET', '/status_view', get_statuses_view)
app.router.add_route('POST', '/create', create_new_report)
