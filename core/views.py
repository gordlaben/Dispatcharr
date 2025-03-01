# core/views.py
import os
import sys
import subprocess
import logging
import re
import redis

from django.conf import settings
from django.http import StreamingHttpResponse, HttpResponseServerError
from django.shortcuts import render

from apps.channels.models import Channel, Stream
from apps.m3u.models import M3UAccountProfile
from core.models import StreamProfile

# Import the persistent lock (the “real” lock)
from dispatcharr.persistent_lock import PersistentLock

# Configure logging to output to the console.
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger(__name__)


def settings_view(request):
    """
    Renders the settings page.
    """
    return render(request, 'settings.html')


def stream_view(request, stream_id):
    """
    Streams the first available stream for the given channel.
    It uses the channel’s assigned StreamProfile.
    A persistent Redis lock is used to prevent concurrent streaming on the same channel.
    """
    try:
        # Retrieve the channel by the provided stream_id.
        channel = Channel.objects.get(channel_number=stream_id)
        logger.debug("Channel retrieved: ID=%s, Name=%s", channel.id, channel.channel_name)

        # Ensure the channel has at least one stream.
        if not channel.streams.exists():
            logger.error("No streams found for channel ID=%s", channel.id)
            return HttpResponseServerError("No stream found for this channel.")

        # Get the first available stream.
        stream = channel.streams.first()
        logger.debug("Using stream: ID=%s, Name=%s", stream.id, stream.name)

        # Retrieve the M3U account associated with the stream.
        m3u_account = stream.m3u_account
        logger.debug("Using M3U account ID=%s, Name=%s", m3u_account.id, m3u_account.name)

        # Use the custom URL if available; otherwise, use the standard URL.
        input_url = stream.custom_url or stream.url
        logger.debug("Input URL: %s", input_url)

        # Determine which profile we can use.
        m3u_profiles = m3u_account.profiles.all()
        default_profile = next((obj for obj in m3u_profiles if obj.is_default), None)
        profiles = [obj for obj in m3u_profiles if not obj.is_default]

        active_profile = None
        # -- Loop through profiles and pick the first active one --
        for profile in [default_profile] + profiles:
            logger.debug(f'Checking profile {profile.name}...')
            if not profile.is_active:
                logger.debug('Profile is not active, skipping.')
                continue
            # *** DISABLE FAKE LOCKS: Ignore current_viewers/max_streams check ***
            logger.debug(f"Using M3U profile ID={profile.id} (ignoring viewer count limits)")
            active_profile = M3UAccountProfile.objects.get(id=profile.id)
            # Prepare the pattern replacement.
            logger.debug("Executing the following pattern replacement:")
            logger.debug(f"  search: {profile.search_pattern}")
            safe_replace_pattern = re.sub(r'\$(\d+)', r'\\\1', profile.replace_pattern)
            logger.debug(f"  replace: {profile.replace_pattern}")
            logger.debug(f"  safe replace: {safe_replace_pattern}")
            stream_url = re.sub(profile.search_pattern, safe_replace_pattern, input_url)
            logger.debug(f"Generated stream url: {stream_url}")
            break

        if active_profile is None:
            logger.exception("No available profiles for the stream")
            return HttpResponseServerError("No available profiles for the stream")

        # Get the stream profile set on the channel.
        stream_profile = channel.stream_profile
        if not stream_profile:
            logger.error("No stream profile set for channel ID=%s", channel.id)
            return HttpResponseServerError("No stream profile set for this channel.")
        logger.debug("Stream profile used: %s", stream_profile.profile_name)

        # Determine the user agent to use.
        user_agent = stream_profile.user_agent or getattr(settings, "DEFAULT_USER_AGENT", "Mozilla/5.0")
        logger.debug("User agent: %s", user_agent)

        # Substitute placeholders in the parameters template.
        parameters = stream_profile.parameters.format(userAgent=user_agent, streamUrl=stream_url)
        logger.debug("Formatted parameters: %s", parameters)

        # Build the final command.
        cmd = [stream_profile.command] + parameters.split()
        logger.debug("Executing command: %s", cmd)

        # Acquire the persistent Redis lock.
        redis_host = getattr(settings, "REDIS_HOST", "localhost")
        redis_client = redis.Redis(host=settings.REDIS_HOST, port=6379, db=0)
        lock_key = f"lock:channel:{channel.id}"
        persistent_lock = PersistentLock(redis_client, lock_key, lock_timeout=120)

        if not persistent_lock.acquire():
            logger.error("Could not acquire persistent lock for channel %s", channel.id)
            return HttpResponseServerError("Resource busy, please try again later.")

        try:
            # Start the streaming process.
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            persistent_lock.release()  # Ensure the lock is released on error.
            logger.exception("Error starting stream for channel ID=%s", stream_id)
            return HttpResponseServerError(f"Error starting stream: {e}")

    except Exception as e:
        logger.exception("Error preparing stream for channel ID=%s", stream_id)
        return HttpResponseServerError(f"Error preparing stream: {e}")

    def stream_generator(proc, s, persistent_lock):
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.terminate()
                logger.debug("Streaming process terminated for stream ID=%s", s.id)
            except Exception as e:
                logger.error("Error terminating process for stream ID=%s: %s", s.id, e)
            persistent_lock.release()
            logger.debug("Persistent lock released for channel ID=%s", channel.id)

    return StreamingHttpResponse(
        stream_generator(process, stream, persistent_lock),
        content_type="video/MP2T"
    )
