import discord
from discord.ext import commands
from dotenv import load_dotenv
import os
from groq import Groq
import io
import json
from groq import AsyncGroq
import time
import aiohttp
from discord.ext import tasks
import asyncio

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, allowed_mentions=discord.AllowedMentions.none())

def hasimage(message: discord.Message) -> bool:
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            return True
    for embed in message.embeds:
        if embed.type in ("image"):
            return True
    return False

async def audio2text(url):
    client = AsyncGroq(api_key=os.environ["GROQAPIKEY"])
    transcription = await client.audio.transcriptions.create(
      url=str(url),
      model="whisper-large-v3",
      temperature=0,
      response_format="verbose_json",
    )
    return str(transcription.text)

times = []
users = {}
cooldown = 10
warns = 5
block = 1800

def check_user(user_id):
    now = time.time()
    d = users.setdefault(user_id, {"last": 0, "warns": 0, "until": 0})
    if now < d["until"]:
        return "ban", int(d["until"] - now) + 1
    if now - d["last"] > 300:
        d["warns"] = 0
    if now- d["last"] < cooldown:
        d["warns"] += 1
        if d["warns"] >= warns:
            d["until"] = now + block
            d["warns"] = 0
            return "new_ban", block
        return "wait", int(cooldown - (now - d["last"])) + 1
    d["last"] = now
    return "ok", 0

def queue_wait():
    now = time.time()
    while times and times[0] < now - 60:
        times.pop(0)
    if len(times) < 20:
        times.append(now)
        return 0
    start = max(times[-20] + 60, times[-1], now)
    if start - now > 120:
        return -1
    times.append(start)
    return int(start - now) + 1

def hasaudio(message):
    grups = [message.attachments]
    ext = (".ogg", ".mp3", ".wav", ".m4a", ".opus", ".flac")
    for snap in message.message_snapshots:
        grups.append(snap.attachments)
    for files in grups:
        for a in files:
            if (a.content_type or "").startswith("audio/"):
                return True
    for word in message.content.split():
        if word.startswith("http"):
            path = word.split("?")[0].lower()
            if path.endswith(ext):
                return True
    return False

def convertsize(size):
    kb = size / 1024
    mb = kb / 1024
    gb = mb / 1024
    if gb >= 1:
        return f"{gb:.2f} GB"
    if mb >= 1:
        return f"{mb:.2f} MB"
    if kb >= 1:
        return f"{kb:.2f} KB"
    return f"{size} bytes"


def save_text(msg_id, i, texto):
    try:
        with open("transcriptions.json", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[f"{msg_id}:{i}"] = {"texto": texto, "t": time.time()}
    with open("transcriptions.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

async def info_link(url):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                return r.content_length
    except Exception:
        return None

def read_text(msg_id, i):
    try:
        with open("transcriptions.json", encoding="utf-8") as f:
            a = json.load(f).get(f"{msg_id}:{i}")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if isinstance(a, dict):
        return a["texto"]
    return a

def loading():
    a = discord.ui.LayoutView(timeout=None)
    a.add_item(discord.ui.Container(discord.ui.TextDisplay("-# Loading...")))
    return a

def waiting():
    a = discord.ui.LayoutView(timeout=None)
    a.add_item(discord.ui.Container(discord.ui.TextDisplay("-# Wait...")))
    return a

@tasks.loop(hours=1)
async def limpiar():
    now = time.time()
    try:
        with open("transcriptions.json", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = None
    if data is not None:
        new = {}
        for k, a in data.items():
            if isinstance(a, dict) and now - a["t"] < 7 * 86400:
                new[k] = a
        if len(new) != len(data):
            with open("transcriptions.json", "w", encoding="utf-8") as f:
                json.dump(new, f, ensure_ascii=False)
    for uid in list(users):
        d = users[uid]
        if now - d["last"] > 3600 and now > d["until"]:
            del users[uid]

class Retry(discord.ui.DynamicItem[discord.ui.Button], template=r"retry:(?P<msg>\d+):(?P<i>\d+)"):
    def __init__(self, msg_id, i):
        super().__init__(discord.ui.Button(label="Retry", style=discord.ButtonStyle.secondary, custom_id=f"retry:{msg_id}:{i}"))
        self.msg_id = msg_id
        self.i = i

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["msg"]), int(match["i"]))

    async def callback(self, interaction):
        status, seg = check_user(interaction.user.id)
        if status != "ok":
            await interaction.response.send_message("Slow down, try again in a moment.", ephemeral=True)
            return
        ref = interaction.message.reference
        if ref is None:
            await interaction.response.send_message("Sorry, i don't see the original file", ephemeral=True)
            return
        wait = queue_wait()
        if wait == -1:
            await interaction.response.send_message("The bot is very busy right now, try again in a few minutes.", ephemeral=True)
            return
        await interaction.response.edit_message(view=waiting() if wait > 0 else loading())
        if wait > 0:
            await asyncio.sleep(wait)
            await interaction.edit_original_response(view=loading())
        try:
            audio_msg = await interaction.channel.fetch_message(ref.message_id)
        except discord.NotFound:
            await interaction.followup.send("Sorry, the original file was deleted!", ephemeral=True)
            return
        audios = await find_voice_messages(bot, audio_msg.channel.id, audio_msg.id, include_reply=False)
        new = await new_view(audios, self.msg_id)
        await interaction.edit_original_response(view=new, allowed_mentions=discord.AllowedMentions.none())

class Extend(discord.ui.DynamicItem[discord.ui.Button], template=r"extend:(?P<msg>\d+):(?P<i>\d+)"):
    def __init__(self, msg_id, i):
        super().__init__(discord.ui.Button(label="Extend", style=discord.ButtonStyle.secondary, custom_id=f"extend:{msg_id}:{i}"))
        self.msg_id = msg_id
        self.i = i

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["msg"]), int(match["i"]))

    async def callback(self, interaction):
        texto = read_text(self.msg_id, self.i)
        if texto is None:
            await interaction.response.send_message("Sorry, i can't get the text", ephemeral=True)
            return
        archivo = discord.File(io.BytesIO(texto.encode("utf-8")), filename="transcription.txt")
        await interaction.response.send_message(file=archivo)

async def new_view(audios, msg_id):
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    container.add_item(discord.ui.TextDisplay("## Transcription"))
    peraudio = 3500 // max(1, len(audios))

    for i, audio in enumerate(audios):
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

        if audio["size"] >= 20447232:
            container.add_item(discord.ui.TextDisplay(f"**{audio['filename']}**\n> **The file is too big** ({convertsize(audio["size"])}) **19.5MB max**"))
            continue

        button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Retry", custom_id=f"retry_{i}")
        container.add_item(discord.ui.Section(f"**{audio['filename']}**", accessory=Retry(msg_id, i)))
        

        try:
            text = await audio2text(audio["url"])
        except Exception as e:
            print("ERROR groq:", repr(e))
            container.add_item(discord.ui.TextDisplay("> Sorry, the transcription failed. Press Retry."))
            continue
        if len(text) > peraudio:
            short = text[:peraudio] + "..."
            save_text(msg_id, i, text)
            container.add_item(discord.ui.Section(f"> {short}", accessory=Extend(msg_id, i)))
        else:
            container.add_item(discord.ui.TextDisplay(f"> {text}"))

    view.add_item(container)
    return view

async def find_voice_messages(bot, channel_id, message_id, include_reply=False):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    message = await channel.fetch_message(message_id)
    groups = []
    groups.append((message.attachments, message.flags.voice, "message", message.jump_url))
    for snap in message.message_snapshots:
        groups.append((snap.attachments, snap.flags.voice, "forward", message.jump_url))

    if include_reply and message.reference is not None:
        original = message.reference.resolved
        if not isinstance(original, discord.Message) and message.reference.message_id is not None:
            try:
                original = await channel.fetch_message(message.reference.message_id)
            except discord.HTTPException:
                original = None
        if isinstance(original, discord.Message):
            groups.append((original.attachments, original.flags.voice, "reply", original.jump_url))
            for snap in original.message_snapshots:
                groups.append((snap.attachments, snap.flags.voice, "forward", original.jump_url))

    results = []
    seen_ids = []
    for attachments, voice_flag, origin, jump_url in groups:
        for a in attachments:
            content_type = a.content_type or ""
            is_audio = content_type.startswith("audio/")
            is_voice = a.is_voice_message() or is_audio

            if is_voice and a.id not in seen_ids:
                seen_ids.append(a.id)
                results.append({
                    "url": a.url,
                    "filename": a.filename,
                    "duration": a.duration,
                    "size": a.size,
                    "origin": origin,
                    "jump_url": jump_url,
                })

    ext = (".ogg", ".mp3", ".wav", ".m4a", ".opus", ".flac")
    texts = [message.content]
    for snap in message.message_snapshots:
        texts.append(snap.content)
    for text in texts:
        for word in text.split():
            path = word.split("?")[0].lower()
            if word.startswith("https") and path.endswith(ext):
                size = await info_link(word)
                if size is not None:
                    results.append({
                        "url": word,
                        "filename": path.split("/")[-1],
                        "duration": None,
                        "size": size,
                        "origin": "link",
                        "jump_url": message.jump_url,
                    })
    return results



@bot.event
async def on_message(message):
    code="text pls"
    if message.author == bot.user:
        return
    if message.author.bot:
        return

    if message.reference is None:
        return

    if code.lower() == message.content.lower():
     reply_msg = message.reference.resolved
     if reply_msg is None or isinstance(reply_msg, discord.DeletedReferencedMessage):
         try:
             reply_msg = await message.channel.fetch_message(message.reference.message_id)
         except discord.NotFound:
             return
     class loading(discord.ui.LayoutView):    
      container1 = discord.ui.Container(
          discord.ui.TextDisplay(content="-# Loading..."),
      )
     if hasaudio(reply_msg): 
      estatus, seg = check_user(message.author.id)
      if estatus == "ban_nuevo":
          await message.reply(f"Too many requests, you are blocked for {seg // 60} minutes.", mention_author=False, silent=True)
          return
      if estatus == "ban":
          return
      wait = queue_wait()
      if wait == -1:
          await message.reply("The bot is very busy right now, try again in a few minutes.", mention_author=False, silent=True)
          return
      msg = await reply_msg.reply(view=waiting() if wait > 0 else loading(), mention_author=False, silent=True)
      if wait > 0:
          await asyncio.sleep(wait)
          await msg.edit(view=loading())
      audios = await find_voice_messages(bot, reply_msg.channel.id, reply_msg.id)
      view2 = await new_view(audios, msg.id)
      await msg.edit(view=view2, allowed_mentions=discord.AllowedMentions.none())

         #text=await audio2text(audio["url"])
     if hasimage(reply_msg):
         print()
              
         


         
     #await message.channel.send(
     #    f"{message.author.mention} respondió a {reply_msg.author.mention}: {reply_msg.content}"
     #)    

async def setup_hook():
    bot.add_dynamic_items(Extend, Retry)
    limpiar.start()

bot.setup_hook = setup_hook

bot.run(os.environ.get("DISCORDTOKEN"))