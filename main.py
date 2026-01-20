import discord
from discord import app_commands
from discord.ext import tasks
import os, json, requests, aiohttp, re, gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
from bs4 import BeautifulSoup

# --- Flask Server ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run).start()

# --- 設定 ---
JST = timezone(timedelta(hours=9))
SHEET_NAME = "AtCoderBot_DB"

def get_rated_color(rating_str):
    if "All" in rating_str: return 0xFF0000 
    match = re.search(r'(\d+)', rating_str)
    if not match: return 0x000000 
    val = int(match.group(1))
    if val < 1200: return 0x008000 
    if val < 2000: return 0x0000FF 
    if val < 2800: return 0xFF8000 
    return 0xFF0000 

class AtCoderBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_data = {}
        self.news_config = {}
        self.problems_map = {}
        self.diff_map = {}
        self.last_sub_ids = set()
        self.sent_notifications = set()
        
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open(SHEET_NAME)
        except Exception as e: print(f"⚠️ Sheetsエラー: {e}")

    # --- DB保存・復元 ---
    def save_to_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            ws_user.clear()
            ws_user.append_row(["GuildID", "AtCoderID", "DiscordID", "ChannelID", "OnlyAC"])
            rows = [[str(v['guild_id']), v['atcoder_id'], str(v['discord_user_id']), str(v['channel_id']), str(v['only_ac'])] for v in self.user_data.values()]
            if rows: ws_user.append_rows(rows)
            ws_config = self.sheet.worksheet("config")
            ws_config.clear()
            ws_config.append_row(["GuildID", "ChannelID"])
            rows_config = [[str(gid), str(cid)] for gid, cid in self.news_config.items()]
            if rows_config: ws_config.append_rows(rows_config)
            print("✅ Sheets保存完了")
        except Exception as e: print(f"❌ 書き込み失敗: {e}")

    def load_from_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            for r in ws_user.get_all_records():
                key = f"{r['GuildID']}_{r['AtCoderID']}"
                self.user_data[key] = {"guild_id": int(r['GuildID']), "atcoder_id": r['AtCoderID'], "discord_user_id": int(r['DiscordID']), "channel_id": int(r['ChannelID']), "only_ac": str(r['OnlyAC']).lower() == 'true'}
            ws_config = self.sheet.worksheet("config")
            for r in ws_config.get_all_records(): self.news_config[str(r['GuildID'])] = int(r['ChannelID'])
            print("✅ 復元完了")
        except Exception as e: print(f"❌ 読み込み失敗: {e}")

    async def setup_hook(self):
        self.load_from_sheets()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://kenkoooo.com/atcoder/resources/problems.json") as r:
                    if r.status == 200: self.problems_map = {x['id']: x['title'] for x in await r.json()}
                async with session.get("https://kenkoooo.com/atcoder/resources/problem-models.json") as r:
                    if r.status == 200: self.diff_map = await r.json()
        except: pass
        self.check_submissions.start()
        self.auto_contest_scheduler.start()
        await self.tree.sync()

    async def fetch_points(self, session, url):
        try:
            async with session.get(url) as resp:
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                p_tag = soup.find(string=re.compile("配点|Score"))
                if p_tag:
                    parent = p_tag.find_parent(["section", "div", "h3"])
                    if parent: return parent.get_text(separator=" ", strip=True).replace("配点", "").strip()
                return "詳細はコンテストページをご確認ください"
        except: return "取得不可"

    # --- 自動スケジューラ (ABC/ARC/AGC/Xmas等すべて対応) ---
    @tasks.loop(minutes=1)
    async def auto_contest_scheduler(self):
        now = datetime.now(JST)
        async with aiohttp.ClientSession() as session:
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200: return
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                table = soup.find('div', id='contest-table-upcoming')
                if not table: return
                for row in table.find_all('tr')[1:]:
                    cols = row.find_all('td')
                    st_dt = datetime.strptime(cols[0].text.strip(), '%Y-%m-%d %H:%M:%S%z')
                    name_tag = cols[1].find('a')
                    c_name, c_url = name_tag.text, "https://atcoder.jp" + name_tag['href']
                    duration, rated = cols[2].text.strip(), cols[3].text.strip()
                    if timedelta(hours=23, minutes=59) < (st_dt - now) <= timedelta(hours=24):
                        await self.broadcast_contest(session, c_name, c_url, st_dt, duration, rated, "⏰ 24時間前告知")
                    if timedelta(minutes=29) < (st_dt - now) <= timedelta(minutes=30):
                        await self.broadcast_contest(session, c_name, c_url, st_dt, duration, rated, "⚠️ コンテスト30分前", is_30min=True)
                    if timedelta(seconds=0) <= (now - st_dt) < timedelta(minutes=1):
                        await self.broadcast_contest(session, c_name, c_url, st_dt, duration, rated, "🚀 コンテスト開始！", is_start=True)
                    try:
                        h, m = map(int, duration.split(':'))
                        if timedelta(seconds=0) <= (now - (st_dt + timedelta(hours=h, minutes=m))) < timedelta(minutes=1):
                            await self.broadcast_contest(session, c_name, c_url, st_dt, duration, rated, "🏁 コンテスト終了！", is_end=True)
                    except: pass

    async def broadcast_contest(self, session, name, url, st, dur, rated, label, is_30min=False, is_start=False, is_end=False):
        task_key = f"{label}_{url}"
        if task_key in self.sent_notifications: return
        self.sent_notifications.add(task_key)
        embed = discord.Embed(title=name, url=url, color=get_rated_color(rated))
        if is_30min:
            pts = await self.fetch_points(session, url)
            embed.description = (f"**コンテストまで残り30分となりました**\n\nコンテスト名：[{name}]({url})\n"
                                 f"👉 [参加登録する]({url})\nレーティング変化： {rated}\n配点： {pts}")
        elif is_start:
            embed.description = f"🚀 **開始時刻となりました！**\n\n📈 [順位表]({url}/standings) | 📝 [自分の提出]({url}/submissions/me)"
        elif is_end:
            embed.description = "🏁 終了時刻となりました。お疲れ様でした！"
        else:
            embed.description = f"⏰ **24時間後に開始します**\n開始：{st.strftime('%Y-%m-%d %H:%M')}\nRated：{rated}"
        for gid, cid in self.news_config.items():
            channel = self.get_channel(cid)
            if channel: await channel.send(content=f"**{label}**", embed=embed)

    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for key, info in list(self.user_data.items()):
                atcoder_id = info['atcoder_id']
                url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={int(datetime.now().timestamp() - 600)}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        for sub in await resp.json():
                            if info.get('only_ac', True) and sub['result'] != 'AC': continue
                            sub_key = f"{info['guild_id']}_{atcoder_id}_{sub['id']}"
                            if sub_key not in self.last_sub_ids:
                                self.last_sub_ids.add(sub_key)
                                await self.send_ac_notification(info, sub)

    async def send_ac_notification(self, info, sub):
        channel = self.get_channel(info['channel_id'])
        if not channel: return
        prob_title = self.problems_map.get(sub['problem_id'], sub['problem_id'])
        embed = discord.Embed(description=f"**[{prob_title}](https://atcoder.jp/contests/{sub['contest_id']}/tasks/{sub['problem_id']})** | **[{sub['result']}]**", color=0x00FF00)
        embed.set_author(name=info['atcoder_id'])
        await channel.send(embed=embed)

bot = AtCoderBot()

# --- 手動コマンド群 (すべて復活) ---

@bot.tree.command(name="register", description="通知設定を登録")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    await interaction.response.defer()
    bot.user_data[f"{interaction.guild_id}_{atcoder_id}"] = {"guild_id": interaction.guild_id, "discord_user_id": discord_user.id, "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac}
    bot.save_to_sheets(); await interaction.followup.send(f"✅ `{atcoder_id}` 登録完了")

@bot.tree.command(name="delete", description="登録解除")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    await interaction.response.defer()
    key = f"{interaction.guild_id}_{atcoder_id}"
    if key in bot.user_data:
        del bot.user_data[key]
        bot.save_to_sheets(); await interaction.followup.send(f"🗑️ `{atcoder_id}` 解除完了")
    else: await interaction.followup.send("登録なし", ephemeral=True)

@bot.tree.command(name="notice_set", description="告知先チャンネル設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer()
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_to_sheets(); await interaction.followup.send(f"✅ 告知先を {channel.mention} に設定")

@bot.tree.command(name="notice_delete", description="告知設定解除")
async def notice_delete(interaction: discord.Interaction):
    await interaction.response.defer()
    gid = str(interaction.guild_id)
    if gid in bot.news_config:
        del bot.news_config[gid]
        bot.save_to_sheets(); await interaction.followup.send("🗑️ 告知設定解除")
    else: await interaction.followup.send("設定なし", ephemeral=True)

@bot.tree.command(name="info", description="予定確認")
async def info(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        async with session.get("https://atcoder.jp/home?lang=ja") as resp:
            soup = BeautifulSoup(await resp.text(), 'html.parser')
            embeds = []
            table = soup.find('div', id='contest-table-upcoming')
            if table:
                for row in table.find_all('tr')[1:4]:
                    cols = row.find_all('td')
                    name_tag = cols[1].find('a')
                    embeds.append(discord.Embed(title=name_tag.text, url="https://atcoder.jp"+name_tag['href']).add_field(name="開始", value=cols[0].text))
            await interaction.followup.send(embeds=embeds if embeds else "予定なし")

@bot.tree.command(name="test_abc441", description="通知一式のテスト")
async def test_abc441(interaction: discord.Interaction):
    await interaction.response.defer()
    url = "https://atcoder.jp/contests/abc441"
    start_dt = datetime.now(JST) + timedelta(seconds=10)
    e1 = discord.Embed(title="ABC441 告知テスト", url=url, color=0xFF0000, description=f"開始：{start_dt.strftime('%H:%M')}\n配点：100-200-300-400-450-500-575")
    e2 = discord.Embed(title="ABC441 開始テスト", url=url, color=0xFF0000, description=f"🚀 開始！\n📈 [順位表]({url}/standings)")
    await interaction.followup.send("🧪 テスト送信:")
    await interaction.channel.send(embed=e1)
    await interaction.channel.send(embed=e2)

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_TOKEN"))
