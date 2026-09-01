# Copyright 2022 Thijs Raymakers
#
# Licensed under the EUPL, Version 1.2 or – as soon they
# will be approved by the European Commission - subsequent
# versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the
# Licence.
# You may obtain a copy of the Licence at:
#
# https://joinup.ec.europa.eu/software/page/eupl
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the Licence is
# distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the Licence for the specific language governing
# permissions and limitations under the Licence.

import asyncio
import re
import sys
import logging
from os import getenv
from podimo.client import PodimoClient
from feedgen.feed import FeedGenerator
from mimetypes import guess_type
from aiohttp import ClientSession, CookieJar, ClientTimeout
from quart import Quart, Response, render_template, request, redirect
from hashlib import sha256
from hypercorn.config import Config
from hypercorn.asyncio import serve
from urllib.parse import quote
from podimo.config import *
from podimo.utils import generateHeaders, randomHexId, set_itunes_image, is_hls_url
import podimo.cache as cache
import cloudscraper
import traceback

# Setup Quart, used for serving the web pages
app = Quart(__name__)
proxies = dict()

#Setup logging
logging.basicConfig(
    format="%(levelname)s | %(asctime)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
)

def example():
    return f"""Example
------------
Username: example@example.com
Password: this-is-my-password
Podcast ID: 12345-abcdef

The URL will be
https://example%40example.com:this-is-my-password@{PODIMO_HOSTNAME}/feed/12345-abcdef.xml

Note that the username and password should be URL encoded. This can be done with
a tool like https://gchq.github.io/CyberChef/#recipe=URL_Encode(true)
"""

@app.after_request
def allow_cors(response):
    response.headers.set('Access-Control-Allow-Origin', '*')
    response.headers.set('Access-Control-Allow-Methods', 'GET, POST')
    response.headers.set('Cache-Control', 'max-age=900')
    logging.debug(f"Incoming {request.method} request for '{request.url}' from User-Agent {request.user_agent} at {request.remote_addr}.")
    return response

def authenticate():
    return Response(
        f"""401 Unauthorized.
You need to login with the correct credentials for Podimo.

{example()}""",
        401,
        {
            "Content-Type": "text/plain",
            "WWW-Authenticate": "Basic realm='Podimo credentials'"
        },
    )

def initialize_client(username: str, password: str, region: str, locale: str) -> PodimoClient:
    client = PodimoClient(username, password, region, locale)

    # Check if there is an authentication token already in memory. If so, use that one.
    # If it is expired, request a new token.
    key = client.key
    client.token = cache.getCacheEntry(key, cache.TOKENS)

    # Check if we previously created a cookie jar
    if key not in cache.cookie_jars:
        cache.cookie_jars[key] = CookieJar()
    client.cookie_jar = cache.cookie_jars[key]
    return client

async def check_auth(username, password, region, locale, scraper):
    try:
        client = initialize_client(username, password, region, locale)
        if client.token:
            return client

        await client.podimoLogin(scraper)
        cache.insertIntoTokenCache(client.key, client.token)
        return client

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        if DEBUG:
            traceback.print_exc()
    return None

podcast_id_pattern = re.compile(r"[0-9a-fA-F\-]+")

@app.route("/", methods=["POST", "GET"])
async def index():
    error = ""
    if request.method == "POST":
        form = await request.form
        email = form.get("email")
        password = form.get("password")
        podcast_id = form.get("podcast_id")
        region = form.get("region")
        locale = form.get("locale")

        if not LOCAL_CREDENTIALS:
            if email is None or email == "":
                error += "Email is required"
            if password is None or password == "":
                error += "Password is required"
        if podcast_id is None or podcast_id == "":
            error += "Podcast ID is required"
        elif podcast_id_pattern.fullmatch(podcast_id) is None:
            error += "Podcast ID is not valid"
        if region is None or region == "":
            error += "Region is required"
        elif region not in [region_code for (region_code, _) in REGIONS]:
            error += "Region is not valid"
        if locale is None or locale == "":
            error += "Locale is required"
        elif locale not in LOCALES:
            error += "Locale is not valid"

        if error == "":
            podcast_id = quote(str(podcast_id), safe="")
            region = quote(str(region), safe="")
            locale = quote(str(locale), safe="")
            
            if LOCAL_CREDENTIALS:
                url = f"{PODIMO_PROTOCOL}://{PODIMO_HOSTNAME}/feed/{podcast_id}.xml?{randomHexId(10)}&region={region}&locale={locale}"
            else:
                email = quote(str(email), safe="")
                comma = quote(',', safe="")
                username = f"{email}{comma}{region}{comma}{locale}"
                password = quote(str(password), safe="")             
                url = f"{PODIMO_PROTOCOL}://{username}:{password}@{PODIMO_HOSTNAME}/feed/{podcast_id}.xml?{randomHexId(10)}&region={region}&locale={locale}"
            
            logging.debug(f"Created an URL: {url}.")
            return await render_template("feed_location.html", url=url)

    return await render_template("index.html", error=error, locales=LOCALES, regions=REGIONS, need_credentials=not(LOCAL_CREDENTIALS))


@app.errorhandler(404)
async def not_found(error):
    return Response(
        f"404 Not found.\n\n{example()}", 404, {"Content-Type": "text/plain"}
    )


@app.route("/feed/<string:podcast_id>.xml")
async def serve_basic_auth_feed(podcast_id):
    if LOCAL_CREDENTIALS:
        args = request.args
        region = args.get("region")
        locale = args.get("locale")
        return await serve_feed(PODIMO_EMAIL, PODIMO_PASSWORD, podcast_id, region, locale)
    else:
        auth = request.authorization
        if not auth:
            return authenticate()
        else:
            username, region, locale = split_username_region_locale(auth.username)
            return await serve_feed(username, auth.password, podcast_id, region, locale)


def split_username_region_locale(string):
    s = string.split(',')
    if len(s) == 3:
        return tuple(s)
    else:
        return (s[0], 'nl', 'nl-NL')


def token_key(username, password):
    key = sha256(
        b"~".join([username.encode("utf-8"), password.encode("utf-8")])
    ).hexdigest()
    return key


@app.route("/feed/<string:username>/<string:password>/<string:podcast_id>.xml")
async def serve_feed(username, password, podcast_id, region, locale):
    
    logging.debug(f"Feed request for podcast {podcast_id} from IP {request.remote_addr} with User-Agent:{request.user_agent}.")
    
    # Check if it is a valid podcast id string
    if podcast_id_pattern.fullmatch(podcast_id) is None:
        return Response("Invalid podcast id format", 400, {})
   
    if region not in [region_code for (region_code, _) in REGIONS]:
        return Response("Invalid region", 400, {})
    if locale not in LOCALES:
        return Response("Invalid locale", 400, {})

    # Check if url contains unique ID or podcastID in blocked list. If so, return HTTP code 410 GONE
    if any(item in request.url for item in BLOCKED):
        logging.debug(f"Blocked! Podcast {podcast_id} is on local block list")
        return Response("Podcast is gone", 410, {}) 
    
    with cloudscraper.create_scraper() as scraper:
        scraper.proxies = proxies
        client = await check_auth(username, password, region, locale, scraper)
        if not client:
            return authenticate()

        # Get a list of valid podcasts
        try:
            podcasts = await podcastsToRss(
                podcast_id, await client.getPodcasts(podcast_id, scraper), locale,
                username, password, region
            )
        except Exception as e:
            exception = str(e)
            if "Podcast not found" in exception:
                return Response(
                    "Podcast not found. Are you sure you have the correct ID?", 404, {}
                )
            logging.error(f"Error while fetching podcasts: {exception}")
            return Response("Something went wrong while fetching the podcasts", 500, {})
        return Response(podcasts, mimetype="text/xml")


@app.route("/audio/<string:podcast_id>/<string:episode_id>.mp3")
async def serve_basic_auth_audio(podcast_id, episode_id):
    if LOCAL_CREDENTIALS:
        args = request.args
        region = args.get("region")
        locale = args.get("locale")
        return await serve_audio(PODIMO_EMAIL, PODIMO_PASSWORD, podcast_id, episode_id, region, locale)
    else:
        auth = request.authorization
        if not auth:
            return authenticate()
        else:
            username, region, locale = split_username_region_locale(auth.username)
            return await serve_audio(username, auth.password, podcast_id, episode_id, region, locale)


@app.route("/audio/<string:username>/<string:password>/<string:podcast_id>/<string:episode_id>.mp3")
async def serve_audio(username, password, podcast_id, episode_id, region=None, locale=None):
    if region is None:
        region = request.args.get("region")
    if locale is None:
        locale = request.args.get("locale")

    if podcast_id_pattern.fullmatch(podcast_id) is None:
        return Response("Invalid podcast id format", 400, {})
    if region not in [region_code for (region_code, _) in REGIONS]:
        return Response("Invalid region", 400, {})
    if locale not in LOCALES:
        return Response("Invalid locale", 400, {})
    if any(item in request.url for item in BLOCKED):
        return Response("Podcast is gone", 410, {})

    async def resolve_hls_url(force_refresh=False):
        """Resolve the episode's current audio URL. Uses its own scraper so it
        also works when called from inside the streaming response body, after
        this request's own scraper context has already closed."""
        with cloudscraper.create_scraper() as scraper:
            scraper.proxies = proxies
            client = await check_auth(username, password, region, locale, scraper)
            if not client:
                raise PermissionError("authentication failed")
            if force_refresh:
                cache.podcast_cache.pop(podcast_id, None)
            data = await client.getPodcasts(podcast_id, scraper)
        episode = next((e for e in data["episodes"] if e["id"] == episode_id), None)
        if episode is None:
            raise LookupError("Episode not found")
        resolved, _ = extract_audio_url(episode)
        if not resolved:
            raise LookupError("No audio available for this episode")
        return resolved, is_hls_url(resolved), episode.get("title") or episode_id

    try:
        url, hls, title = await resolve_hls_url()
    except PermissionError:
        return authenticate()
    except LookupError as e:
        return Response(str(e), 404, {})
    except Exception as e:
        logging.error(f"Error while fetching podcast for audio proxy: {e}")
        return Response("Something went wrong while fetching the podcast", 500, {})

    if not hls:
        # Shouldn't normally happen since we only ever point enclosures
        # here for HLS episodes, but handle it gracefully regardless.
        return redirect(url)

    logging.info(f"Transcoding HLS episode '{title}' ({episode_id}) for podcast {podcast_id}")
    body = stream_hls_episode_as_mp3(url, locale, resolve_hls_url)
    return Response(body, mimetype="audio/mpeg")


async def stream_hls_episode_as_mp3(url, locale, resolve_url=None):
    """Transcode a (signed) HLS manifest into a plain MP3 byte stream with
    ffmpeg.

    stderr is drained concurrently so a chatty ffmpeg can't fill the ~64 KiB
    OS pipe buffer and wedge the transcode (which shows up downstream as a
    download that hangs forever); ffmpeg is told to reconnect on transient
    network/5xx errors and to time out a stalled socket read; and a non-zero
    exit is surfaced (by raising) instead of silently truncating the file.
    """
    user_agent = generateHeaders(None, locale)["user-agent"]

    async def start(input_url):
        command = [FFMPEG_PATH, "-nostdin", "-loglevel", "warning"]
        if input_url.lower().startswith(("http://", "https://")):
            command += [
                "-user_agent", user_agent,
                # Fail a stalled network read after 30s instead of hanging forever.
                "-rw_timeout", "30000000",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                # Retry 5xx, but let 4xx (e.g. an expired signed URL) fail fast
                # so the caller can re-resolve and retry with a fresh URL.
                "-reconnect_on_http_error", "5xx",
                "-reconnect_delay_max", "30",
            ]
        command += [
            "-i", input_url,
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "128k",
            "-f", "mp3",
            "pipe:1",
        ]
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def drain_stderr(process, tail):
        """Continuously consume ffmpeg's stderr so it can never fill the OS
        pipe buffer and stall the transcode. Only the most recent output is
        kept, for diagnostics. Fixed-size reads avoid StreamReader's line
        length limit."""
        try:
            while True:
                block = await process.stderr.read(4096)
                if not block:
                    break
                tail.append(block)
                if len(tail) > 64:
                    del tail[: len(tail) - 64]
        except Exception:
            pass

    input_url = url
    produced_any = False
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        process = await start(input_url)
        tail = []
        drainer = asyncio.ensure_future(drain_stderr(process, tail))
        try:
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                produced_any = True
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            drainer.cancel()
            try:
                await drainer
            except BaseException:
                pass

        rc = process.returncode
        if rc is None or rc == 0:
            return
        if rc < 0:
            logging.warning(f"ffmpeg (HLS transcode) terminated by signal {-rc}")
            return

        stderr_tail = b"".join(tail).decode(errors="replace")[-2000:]

        # Only safe to retry from scratch while nothing has been sent to the
        # client yet - a partially-streamed body can't be restarted.
        if not produced_any and attempt < max_attempts and resolve_url is not None:
            logging.warning(
                f"ffmpeg exited {rc} before producing audio; refreshing the "
                f"episode URL and retrying. stderr:\n{stderr_tail}"
            )
            try:
                fresh_url, fresh_is_hls, _ = await resolve_url(force_refresh=True)
            except Exception as e:
                logging.error(f"Could not refresh episode URL: {e}")
            else:
                if fresh_url and fresh_is_hls:
                    input_url = fresh_url
                    continue

        raise RuntimeError(
            f"ffmpeg exited with code {rc} while transcoding HLS episode; "
            f"stderr:\n{stderr_tail}"
        )


async def urlHeadInfo(session, id, url, locale):
    entry = cache.getHeadEntry(id)
    if entry:
        return entry

    retries = 3  # Number of retries
    timeout = ClientTimeout(total=10)  # 10 seconds timeout for each try

    for attempt in range(retries):
        try:
            logging.debug(f"HEAD request to {url} (Attempt {attempt + 1})")
            async with session.head(url, allow_redirects=True,
                                    headers=generateHeaders(None, locale),
                                    timeout=timeout) as response:
                content_length = 0
                content_type, _ = guess_type(url)
                if 'content-length' in response.headers:
                    content_length = response.headers['content-length']
                if content_type is None and 'content-type' in response.headers:
                    content_type = response.headers['content-type']
                else:
                    content_type = 'audio/mpeg'
                cache.insertIntoHeadCache(id, content_length, content_type)
                return (content_length, content_type)

        except asyncio.TimeoutError:
            if attempt < retries - 1:
                logging.info(f"Retrying HEAD request to {url} (Attempt {attempt + 2})")
                await asyncio.sleep(1)  # Wait for 1 second before retrying
            else:
                logging.error(f"All retries failed for HEAD request to {url}")
                raise  # Re-raise the last exception if all retries fail



def extract_audio_url(episode):
    duration = 0
    url = None
    if episode['audio']:
        url = episode['audio']['url']
        duration = episode['audio']['duration']

    if url is None or url == "":
        if episode["streamMedia"]:
            url = episode["streamMedia"]["url"]
            duration = episode["streamMedia"]["duration"]

    if url and is_hls_url(url):
        # Some older Podimo CDN URLs expose a direct MP3 next to the HLS
        # manifest via this string substitution. Newer episodes (signed
        # media-cdn-episodes.podimo.com manifests) don't match this pattern,
        # so it's best-effort; addFeedEntry proxies/transcodes the HLS
        # stream itself when it doesn't apply.
        direct = url.replace("hls-media", "audios").replace("/main.m3u8", ".mp3")
        if direct != url and not is_hls_url(direct):
            url = direct

    return url, duration


def build_audio_proxy_url(podcast_id, episode_id, username, password, region, locale):
    episode_id = quote(str(episode_id), safe="")
    region = quote(str(region), safe="")
    locale = quote(str(locale), safe="")
    path = f"/audio/{podcast_id}/{episode_id}.mp3?region={region}&locale={locale}"
    if LOCAL_CREDENTIALS:
        return f"{PODIMO_PROTOCOL}://{PODIMO_HOSTNAME}{path}"
    username = quote(str(username), safe="")
    password = quote(str(password), safe="")
    return f"{PODIMO_PROTOCOL}://{username}:{password}@{PODIMO_HOSTNAME}{path}"


async def addFeedEntry(fg, episode, session, locale, podcast_id, username, password, region):
    fe = fg.add_entry()
    fe.guid(episode["id"])
    fe.title(episode["title"])
    fe.description(episode["description"])
    fe.pubDate(episode.get("publishDatetime", episode.get("datetime")))
    set_itunes_image(fe.podcast, episode["imageUrl"])

    url, duration = extract_audio_url(episode)
    if url is None:
        return
    logging.debug(f"Found podcast '{episode['title']}'")
    fe.podcast.itunes_duration(duration)

    if is_hls_url(url):
        # Podcast apps (e.g. AudiobookShelf) fetch the enclosure and pipe it
        # straight into ffmpeg with no extension/mime hint, which can't
        # auto-detect an HLS manifest from a raw byte stream and fails with
        # "Invalid data found when processing input". Point the enclosure at
        # our own proxy, which resolves and transcodes the HLS stream into a
        # plain MP3 on the fly instead.
        enclosure_url = build_audio_proxy_url(podcast_id, episode["id"], username, password, region, locale)
        fe.enclosure(enclosure_url, 0, "audio/mpeg")
        return

    content_length, content_type = await urlHeadInfo(session, episode['id'], url, locale)
    fe.enclosure(url, content_length, content_type)

def chunks(x, n):
    for i in range(0, len(x), n):
        yield x[i:i + n]

async def podcastsToRss(podcast_id, data, locale, username, password, region):
    fg = FeedGenerator()
    fg.load_extension("podcast")

    podcast = data["podcast"]
    episodes = data["episodes"]

    if len(episodes) > 0:
        last_episode = episodes[0]
        title = podcast["title"]
        if podcast["title"] is None:
            title = last_episode["podcastName"]
        fg.title(title)

        if podcast["description"]:
            fg.description(podcast["description"])
        else:
            fg.description(title)

        fg.link(href=f"https://podimo.com/shows/{podcast_id}", rel="alternate")

        image = podcast["images"]["coverImageUrl"]
        if image is None:
            image = last_episode['imageUrl']
        fg.image(image)

        language = podcast["language"]
        if language is None:
            language = locale
        fg.language(language)

        artist = podcast["authorName"]
        if artist is None:
            artist = last_episode["artist"]
        fg.podcast.itunes_author(artist)

        if not PUBLIC_FEEDS:
            fg.podcast.itunes_block(True)

    async with ClientSession() as session:
        for chunk in chunks(episodes, 5):
            await asyncio.gather(
                *[addFeedEntry(fg, episode, session, locale, podcast_id, username, password, region) for episode in chunk]
            )

    feed = fg.rss_str(pretty=True)
    return feed


async def spawn_web_server():
    config = Config()
    config.bind = [PODIMO_BIND_HOST]
    config.read_timeout = 60
    config.graceful_timeout = 5
    config.backlog = 1000
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    await serve(app, config)

async def main():
    if HTTP_PROXY:
        global proxies
        logging.info(f"Running with https proxy defined in environmental variable HTTP_PROXY: {HTTP_PROXY}")
        proxies['https'] = HTTP_PROXY
    tasks = [spawn_web_server()]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    if DEBUG:
        logging.info(f"""Spawning server on {PODIMO_BIND_HOST}
Configuration: 
- DEBUG: {DEBUG}
- LOCAL CREDENTIALS: {LOCAL_CREDENTIALS} ({PODIMO_EMAIL})
- PODIMO_HOSTNAME: {PODIMO_HOSTNAME}
- PODIMO_BIND_HOST: {PODIMO_BIND_HOST}
- PODIMO_PROTOCOL: {PODIMO_PROTOCOL}
- PUBLIC_FEEDS: {PUBLIC_FEEDS}
- HTTP_PROXY: {HTTP_PROXY}
- ZENROWS_API: {ZENROWS_API}
- SCRAPER_API: {SCRAPER_API}
- CACHE_DIR: {CACHE_DIR}
- STORE_TOKENS_ON_DISK: {STORE_TOKENS_ON_DISK}
- TOKEN_CACHE_TIME: {TOKEN_CACHE_TIME} sec
- PODCAST_CACHE_TIME: {PODCAST_CACHE_TIME} sec
- HEAD_CACHE_TIME: {HEAD_CACHE_TIME} sec
- BLOCKING: {BLOCKED}
""")
    asyncio.run(main())
