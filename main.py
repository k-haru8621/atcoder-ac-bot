import discord
from discord import app_commands
from discord.ext import tasks
import os
import json
import requests
import aiohttp
from datetime import datetime, timedelta, timezone, time
from flask import Flask
from threading import Thread
from bs4 import BeautifulSoup

# --- Flask Server (Renderスリープ防止) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run).start()

DATA_FILE = "users.json"
# コンテストの丸の色判定用
CIRCLE_COLORS = {
    "blue": 0x0000FF, "red": 0xFF0000, "orange": 0xFF8000,
    "yellow": 0xFFCC00, "green": 0x008000, "cyan": 0x00C0C0,
    "brown": 0x804000, "gray": 0x808080, "black": 0x000000
}

# 日本時間 (JST) 設定
JST = timezone(timedelta(hours=9))
SCHEDULE_TIME = time(hour=9, minute=0, tzinfo=JST) # 毎朝9時に自動送信

class AtCoderBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_data = self.load_data()
        self.problems_map = {}
        self.diff_map = {}
        self.last_sub_ids = set()

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f: return json.load(f)
            except: return {}
        return {}

    def save_data(self):
        with open(DATA_FILE, "w") as f: json.dump(self.user_data, f)

    def load_atcoder_resources(self):
        try:
            p_res = requests.get("https://kenkoooo.com/atcoder/resources/problems.json").json()
            self.problems_map = {p['id']: p['title'] for p in p_res}
            d_res = requests.get("https://kenkoooo.com/atcoder/resources/problem-models.json").json()
            self.diff_map = d_res
            print("AtCoder API Resources Loaded.")
        except Exception as e: print(f"API load error: {e}")

    async def setup_hook(self):
        self.load_atcoder_resources()
        self.check_submissions.start()
        self.daily_info_task.start()
        await self.tree.sync()

    # --- 公式サイトからの情報取得ロジック ---
    async def fetch_atcoder_info_embeds(self):
        url = "https://atcoder.jp/home"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200: return []
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                embeds = []
                
                # 1. 予定されたコンテスト (丸の色を反映)
                table = soup.find('div', id='contest-table-upcoming')
                if table:
                    for row in table.find_all('tr')[1:4]: # 直近3件
                        cols = row.find_all('td')
                        time_str, name_tag = cols[0].text, cols[1].find('a')
                        img = cols[1].find('img')
                        color = CIRCLE_COLORS["black"]
                        if img:
                            for c in CIRCLE_COLORS:
                                if c in img['src']: color = CIRCLE_COLORS[c]; break
                        e = discord.Embed(title=f"🏆 {name_tag.text}", url="https://atcoder.jp"+name_tag['href'], color=color)
                        e.add_field(name="開始時刻", value=time_str)
                        embeds.append(e)

                # 2. 重要な告知 (緑色)
                important_section = soup.find('div', id='home-important-notices')
                if important_section:
                    notices = [li.text.strip() for li in important_section.find_all('li')[:5]]
                    if notices:
                        e = discord.Embed(title="✅ 重要な告知", description="\n".join([f"• {n}" for n in notices]), color=0x008000)
                        embeds.append(e)

                # 3. 最近の告知 (デフォルト色)
                news_section = soup.find('div', class_='col-md-3')
                if news_section:
                    news_list = [li.text.strip() for li in news_section.find_all('li')[:5]]
                    if news_list:
                        e = discord.Embed(title="📢 最近の告知", description="\n".join([f"• {n}" for n in news_list]), color=0x34495e)
                        embeds.append(e)
                
                return embeds

    # --- 自動送信タスク ---
    @tasks.loop(time=SCHEDULE_TIME)
    async def daily_info_task(self):
        embeds = await self.fetch_atcoder_info_embeds()
        if not embeds: return
        channels = set(info['channel_id'] for info in self.user_data.values())
        for ch_id in channels:
            channel = self.get_channel(ch_id)
            if channel:
                try: await channel.send("☀️ **おはようございます！本日のAtCoder情報です**", embeds=embeds)
                except: pass

    # --- AC通知ループ (3分おき) ---
    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for key, info in list(self.user_data.items()):
                atcoder_id = info['atcoder_id']
                reg_time = info.get('registered_at', datetime.now().timestamp())
                from_time = int(max(reg_time - 86400, datetime.now().timestamp() - 600))
                url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={from_time}"
                async with session.get(url) as resp:
                    if resp.status != 200: continue
                    subs = await resp.json()
                    for sub in subs:
                        if sub['result'] == 'AC':
                            sub_key = f"{info['guild_id']}_{atcoder_id}_{sub['id']}"
                            if sub_key in self.last_sub_ids: continue
                            if sub['epoch_second'] >= (datetime.now().timestamp() - 86400):
                                self.last_sub_ids.add(sub_key)
                                await self.send_notification(info, sub)

    async def send_notification(self, info, sub):
        channel = self.get_channel(info['channel_id'])
        if not channel: return
        member = channel.guild.get_member(info['discord_user_id'])
        user_name = member.display_name if member else "Unknown"
        avatar_url = member.display_avatar.url if member else None
        prob_id = sub['problem_id']
        prob_title = self.problems_map.get(prob_id, prob_id)
        diff_val = self.diff_map.get(prob_id, {}).get('difficulty', '不明')
        color = self.get_difficulty_color(diff_val)
        dt_jst = datetime.fromtimestamp(sub['epoch_second'], JST)
        time_str = dt_jst.strftime('%Y年%m月%d日 %p %I:%M:%S').replace('AM', '午前').replace('PM', '午後')
        embed = discord.Embed(description=f"**[{prob_title}](https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id})** | **[{sub['result']}]** | [📄提出](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})", color=color)
        embed.set_author(name=f"{user_name} / {info['atcoder_id']}", icon_url=avatar_url)
        embed.add_field(name="", value=f"diff：{diff_val} | 言語：{sub['language']} | 実行時間：{sub['execution_time']} ms", inline=False)
        embed.add_field(name="---", value=f"コンテスト: {sub['contest_id'].upper()}", inline=False)
        embed.set_footer(text=f"提出時間：{time_str}")
        await channel.send(embed=embed)

    def get_difficulty_color(self, diff):
        if not isinstance(diff, int): return 0x000000
        if diff < 400: return 0x808080
        if diff < 800: return 0x804000
        if diff < 1200: return 0x008000
        if diff < 1600: return 0x00C0C0
        if diff < 2000: return 0x0000FF
        if diff < 2400: return 0xFFCC00
        if diff < 2800: return 0xFF8000
        return 0xFF0000

bot = AtCoderBot()

@bot.tree.command(name="register", description="通知設定を登録します")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_handle: str, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    unique_key = f"{guild_id}_{atcoder_handle}"
    if unique_key in bot.user_data:
        await interaction.response.send_message(f"❌ `{atcoder_handle}` は既にこのサーバーで登録されています。", ephemeral=True)
        return
    bot.user_data[unique_key] = {
        "guild_id": guild_id, "discord_user_id": discord_user.id,
        "atcoder_id": atcoder_handle, "channel_id": channel.id,
        "registered_at": datetime.now().timestamp()
    }
    bot.save_data()
    await interaction.response.send_message(f"✅ 登録完了!\n**User:** {discord_user.mention}\n**AtCoder:** `{atcoder_handle}`\n(毎朝9時にコンテスト情報を自動送信します)")

@bot.tree.command(name="delete", description="自分の登録を解除します")
async def delete(interaction: discord.Interaction, atcoder_handle: str):
    guild_id = str(interaction.guild_id)
    unique_key = f"{guild_id}_{atcoder_handle}"
    if unique_key not in bot.user_data:
        await interaction.response.send_message(f"❓ `{atcoder_handle}` は未登録です。", ephemeral=True)
        return
    if interaction.user.id == bot.user_data[unique_key]["discord_user_id"] or interaction.user.guild_permissions.manage_messages:
        del bot.user_data[unique_key]
        bot.save_data()
        await interaction.response.send_message(f"🗑️ `{atcoder_handle}` を削除しました。")
    else:
        await interaction.response.send_message(f"❌ 権限がありません。", ephemeral=True)

@bot.tree.command(name="info", description="手動でコンテスト情報を表示します")
async def info(interaction: discord.Interaction):
    await interaction.response.defer()
    embeds = await bot.fetch_atcoder_info_embeds()
    if embeds: await interaction.followup.send(embeds=embeds)
    else: await interaction.followup.send("取得に失敗しました。")

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_TOKEN"))
