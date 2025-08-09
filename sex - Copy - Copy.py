import re
import asyncio
import functools
from typing import List, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp
from youtube_search import YoutubeSearch
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from discord import Embed

afk_users = {}

# -------------------------
# CONFIG - edit these
# -------------------------
BOT_TOKEN = "MTQwMzc4NzI2OTM0NDg1NDI1Nw.GkYtyB.mdU9jPLcchaa8_z0Iw4PpYNSSZ8a6CsGRKNqGo"  # <-- put your regenerated token here
SPOTIFY_CLIENT_ID = "864faf9ad71f4f818bbb9f4e22a3c6c5"
SPOTIFY_CLIENT_SECRET = "4f8f8415e23147deb24ac92d3c629e5b"
COMMAND_PREFIX = "/"
# -------------------------

intents = discord.Intents.default()
intents.message_content = True  # not used for slash commands but keep if you mix prefixed commands
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# yt-dlp options
ytdl_format_options = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "ignoreerrors": True,
    "default_search": "auto",
}

ffmpeg_options = {
    "options": "-vn"
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# Spotify client (client credentials flow)
spotify_auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
)
sp = spotipy.Spotify(auth_manager=spotify_auth_manager)


# Helper: search youtube and return url + title
def search_youtube(query: str) -> Optional[Dict[str, str]]:
    try:
        results = YoutubeSearch(query, max_results=1).to_dict()
        if not results:
            return None
        r = results[0]
        url = f"https://www.youtube.com/watch?v={r['id']}"
        title = r.get("title", query)
        return {"url": url, "title": title}
    except Exception:
        return None


# Converts a youtube/stream url into a playable FFmpeg audio source (wrapped with volume)
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume: float = 0.6):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.webpage_url = data.get("webpage_url")

    @classmethod
    async def from_url(cls, url: str, *, loop: asyncio.AbstractEventLoop, stream=True, volume: float = 0.6):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if data is None:
            raise RuntimeError("ytdl.extract_info returned None")
        if "entries" in data:
            data = data["entries"][0]
        filename = data["url"] if stream else ytdl.prepare_filename(data)
        source = discord.FFmpegPCMAudio(filename, **ffmpeg_options)
        return cls(source, data=data, volume=volume)


# Per-guild music state
class GuildMusic:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: List[Dict] = []  # each item: {"title": str, "url": str, "requester": discord.Member}
        self.current: Optional[Dict] = None
        self.loop_mode = "off"  # off | song | queue
        self.volume = 0.6
        self.play_lock = asyncio.Lock()

    def enqueue(self, item: Dict):
        self.queue.append(item)

    def dequeue(self) -> Optional[Dict]:
        if not self.queue:
            return None
        return self.queue.pop(0)

    def clear(self):
        self.queue.clear()

    def queue_list(self) -> List[str]:
        return [f"{i+1}. {s['title']} (requested by {s['requester'].display_name})" for i, s in enumerate(self.queue)]


music_managers: Dict[int, GuildMusic] = {}  # guild.id -> GuildMusic


def get_guild_music(guild: discord.Guild) -> GuildMusic:
    if guild.id not in music_managers:
        music_managers[guild.id] = GuildMusic(guild)
    return music_managers[guild.id]


# Utility to detect Spotify URLs
SPOTIFY_TRACK_RE = re.compile(r"https?://open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"https?://open\.spotify\.com/playlist/([A-Za-z0-9]+)")
SPOTIFY_ALBUM_RE = re.compile(r"https?://open\.spotify\.com/album/([A-Za-z0-9]+)")


# View (buttons)
class MusicControlView(discord.ui.View):
    def __init__(self, guild_music: GuildMusic, *, timeout=300):
        super().__init__(timeout=timeout)
        self.guild_music = guild_music

    async def _respond_ephemeral(self, interaction: discord.Interaction, content: str):
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            # fallback for followup
            await interaction.followup.send(content, ephemeral=True)

    @discord.ui.button(label="pause", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await self._respond_ephemeral(interaction, "paused")
        else:
            await self._respond_ephemeral(interaction, "nothing is playing.")

    @discord.ui.button(label="resume", style=discord.ButtonStyle.secondary)
    async def resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await self._respond_ephemeral(interaction, "resumed")
        else:
            await self._respond_ephemeral(interaction, "im not paused.")

    @discord.ui.button(label="skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # will trigger after callback to play next
            await self._respond_ephemeral(interaction, "skipped")
        else:
            await self._respond_ephemeral(interaction, "nothing to skip.")

    @discord.ui.button(label="stop", style=discord.ButtonStyle.secondary)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
            await self._respond_ephemeral(interaction, "stopped and disconnected")
        else:
            await self._respond_ephemeral(interaction, "not connected.")


# Core playback logic
async def ensure_voice_connected(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("you need to be in a voice channel.", ephemeral=True)
        return None
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if not vc:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)
    return vc


@bot.tree.command(name="afk", description="Set your AFK status with an optional reason")
@app_commands.describe(reason="Reason for going AFK")
async def afk_command(interaction: discord.Interaction, reason: Optional[str] = None):
    user_id = interaction.user.id
    afk_users[user_id] = reason or "AFK"

    embed = Embed(
        title="bros afk",
        description=f"{interaction.user.mention} this niggas now afk.\n**Reason:** {afk_users[user_id]}",
        color=0xFF0000
    )
    await interaction.response.send_message(embed=embed)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Remove AFK if the AFK user sends a message
    if message.author.id in afk_users:
        afk_users.pop(message.author.id)
        embed = Embed(
            title="unwelcome back nigger",
            description=f"{message.author.mention} is no longer fucking kids.",
            color=0x00FF00
        )
        await message.channel.send(embed=embed)

    # Notify if mentioned user is AFK
    for user in message.mentions:
        if user.id in afk_users:
            reason = afk_users[user.id]
            embed = Embed(
                title=f"🛑 {user.display_name} is afk",
                description=f"reason: {reason}",
                color=0xFF0000
            )
            await message.channel.send(embed=embed)

    await bot.process_commands(message)


async def play_next_in_guild(guild: discord.Guild):
    guild_music = get_guild_music(guild)
    vc = guild.voice_client
    if not vc:
        return

    async with guild_music.play_lock:
        # Handle looping and selecting next
        next_item = None

        if guild_music.current is None:
            # starting fresh
            next_item = guild_music.dequeue()
        else:
            # We just finished playing current
            if guild_music.loop_mode == "song":
                # replay same song
                next_item = guild_music.current
            elif guild_music.loop_mode == "queue":
                # append current to end and pop next
                guild_music.enqueue(guild_music.current)
                next_item = guild_music.dequeue()
            else:
                # off
                next_item = guild_music.dequeue()

        if next_item is None:
            # nothing to play
            guild_music.current = None
            return

        try:
            player = await YTDLSource.from_url(next_item["url"], loop=bot.loop, stream=True, volume=guild_music.volume)
        except Exception as e:
            # skip and try next
            print(f"Error creating player: {e}")
            guild_music.current = None
            await play_next_in_guild(guild)
            return

        guild_music.current = next_item

        def after_play(err):
            if err:
                print("Playback error:", err)
            fut = asyncio.run_coroutine_threadsafe(play_next_in_guild(guild), bot.loop)
            try:
                fut.result()
            except Exception as e:
                print("Error scheduling next track:", e)

        vc.play(player, after=after_play)
        # update volume (ytdl source uses volume property)
        vc.source.volume = guild_music.volume


# Helper: enqueue a single track (url+title) requested by member
def enqueue_track(guild: discord.Guild, title: str, url: str, requester: discord.Member):
    gm = get_guild_music(guild)
    gm.enqueue({"title": title, "url": url, "requester": requester})


# Slash commands
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Failed to sync commands:", e)


@bot.tree.command(name="play", description="Play a song or add to queue. Accepts Spotify URLs, YouTube URLs, or search text.")
@app_commands.describe(query="Spotify/YouTube link or search keywords")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()  # give us more time

    # Must be in VC
    vc = await ensure_voice_connected(interaction)
    if vc is None:
        return

    guild_music = get_guild_music(interaction.guild)

    # Detect spotify links
    track_match = SPOTIFY_TRACK_RE.search(query)
    playlist_match = SPOTIFY_PLAYLIST_RE.search(query)
    album_match = SPOTIFY_ALBUM_RE.search(query)

    to_enqueue = []  # list of (title, url) pairs

    try:
        if track_match:
            # single track
            track_id = track_match.group(1)
            track = sp.track(track_id)
            artist_names = ", ".join([a["name"] for a in track["artists"]])
            name = f"{artist_names} - {track['name']}"
            yt = search_youtube(name)
            if yt:
                to_enqueue.append((yt["title"], yt["url"]))
        elif playlist_match:
            # playlist -> iterate items
            playlist_id = playlist_match.group(1)
            results = sp.playlist_items(playlist_id, additional_types=["track"])
            items = results.get("items", [])
            # handle paging
            while results and results.get("next"):
                results = sp.next(results)
                items.extend(results.get("items", []))
            for item in items:
                track = item.get("track")
                if not track:
                    continue
                artist_names = ", ".join([a["name"] for a in track["artists"]])
                name = f"{artist_names} - {track['name']}"
                yt = search_youtube(name)
                if yt:
                    to_enqueue.append((yt["title"], yt["url"]))
        elif album_match:
            album_id = album_match.group(1)
            results = sp.album_tracks(album_id)
            items = results.get("items", [])
            while results and results.get("next"):
                results = sp.next(results)
                items.extend(results.get("items", []))
            # album tracks don't have full artist list in same way as playlist items, so fetch album to get artist?
            album = sp.album(album_id)
            for t in items:
                artist_names = ", ".join([a["name"] for a in t["artists"]])
                name = f"{artist_names} - {t['name']}"
                yt = search_youtube(name)
                if yt:
                    to_enqueue.append((yt["title"], yt["url"]))
        else:
            # youtube or plain search
            if "youtube.com" in query or "youtu.be" in query:
                # Try to resolve title via yt search helper
                yt = search_youtube(query)
                if yt:
                    to_enqueue.append((yt["title"], yt["url"]))
                else:
                    to_enqueue.append((query, query))
            else:
                yt = search_youtube(query)
                if not yt:
                    await interaction.followup.send("Couldn't find anything on YouTube or Spotify for that query.", ephemeral=True)
                    return
                to_enqueue.append((yt["title"], yt["url"]))
    except spotipy.SpotifyException as e:
        await interaction.followup.send(f"Spotify error: {e}", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"Error processing query: {e}", ephemeral=True)
        return

    # enqueue all found
    for title, url in to_enqueue:
        enqueue_track(interaction.guild, title, url, interaction.user)

    # If nothing is currently playing, start playback
    vc = interaction.guild.voice_client
    if not vc.is_playing() and not vc.is_paused():
        # start next track
        await play_next_in_guild(interaction.guild)

    # Build response
    if len(to_enqueue) == 1:
        title, url = to_enqueue[0]
        embed = discord.Embed(title="Enqueued", description=f"[{title}]({url})", color=0xFFFFFF)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, view=MusicControlView(get_guild_music(interaction.guild)))
    else:
        embed = discord.Embed(title="Enqueued playlist", description=f"Added {len(to_enqueue)} tracks to the queue.", color=0xFFFFFF)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, view=MusicControlView(get_guild_music(interaction.guild)))


@bot.tree.command(name="skip", description="skip the current song")
async def slash_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("nothing is playing.", ephemeral=True)
        return
    vc.stop()
    await interaction.response.send_message("skipped.", ephemeral=True)


@bot.tree.command(name="nowplaying", description="show the currently playing song")
async def slash_nowplaying(interaction: discord.Interaction):
    gm = get_guild_music(interaction.guild)
    if gm.current:
        embed = discord.Embed(title="Now Playing", description=f"{gm.current['title']}", color=discord.Color.blurple())
        embed.add_field(name="Requested by", value=gm.current["requester"].display_name, inline=True)
        embed.add_field(name="Loop", value=gm.loop_mode, inline=True)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("Not playing anything.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the queue")
async def slash_queue(interaction: discord.Interaction):
    gm = get_guild_music(interaction.guild)
    lines = gm.queue_list()
    if not lines:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return
    # paginate if long (simple)
    msg = "\n".join(lines[:15])
    if len(lines) > 15:
        msg += f"\n...and {len(lines)-15} more"
    await interaction.response.send_message(f"Current queue:\n{msg}")


@bot.tree.command(name="volume", description="Set playback volume (0-100)")
@app_commands.describe(level="Volume percent from 0 to 100")
async def slash_volume(interaction: discord.Interaction, level: int):
    if level < 0 or level > 100:
        await interaction.response.send_message("Provide a number between 0 and 100.", ephemeral=True)
        return
    gm = get_guild_music(interaction.guild)
    gm.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and vc.source:
        vc.source.volume = gm.volume
    await interaction.response.send_message(f"Volume set to {level}% (server-wide for this guild).", ephemeral=True)


@bot.tree.command(name="loop", description="Toggle loop mode: off / song / queue")
@app_commands.describe(mode="Options: off, song, queue")
async def slash_loop(interaction: discord.Interaction, mode: str):
    mode = mode.lower()
    if mode not in ("off", "song", "queue"):
        await interaction.response.send_message("Invalid mode. Use 'off', 'song', or 'queue'.", ephemeral=True)
        return
    gm = get_guild_music(interaction.guild)
    gm.loop_mode = mode
    await interaction.response.send_message(f"Loop mode set to {mode}.", ephemeral=True)


@bot.tree.command(name="remove", description="Remove a song from the queue by index (1-based)")
@app_commands.describe(index="Index in the queue (1-based)")
async def slash_remove(interaction: discord.Interaction, index: int):
    gm = get_guild_music(interaction.guild)
    if index < 1 or index > len(gm.queue):
        await interaction.response.send_message("Index out of range.", ephemeral=True)
        return
    item = gm.queue.pop(index - 1)
    await interaction.response.send_message(f"Removed: {item['title']}", ephemeral=True)


@bot.tree.command(name="clear", description="Clear the queue")
async def slash_clear(interaction: discord.Interaction):
    gm = get_guild_music(interaction.guild)
    gm.clear()
    await interaction.response.send_message("Queue cleared.", ephemeral=True)


@bot.tree.command(name="leave", description="Disconnect the bot from voice channel")
async def slash_leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Disconnected.", ephemeral=True)
    else:
        await interaction.response.send_message("I'm not connected.", ephemeral=True)


# Run the bot
if __name__ == "__main__":
    if BOT_TOKEN == "MTQwMzc4NzI2OTM0NDg1NDI1Nw.GkYtyB.mdU9jPLcchaa8_z0Iw4PpYNSSZ8a6CsGRKNqGo":
        print("ERROR: Set your bot token in the script before running.")
    elif SPOTIFY_CLIENT_SECRET == "4f8f8415e23147deb24ac92d3c629e5b":
        print("WARNING: Spotify client secret not set; Spotify links WILL FAIL. Set SPOTIFY_CLIENT_SECRET in the script.")
    bot.run(BOT_TOKEN)

