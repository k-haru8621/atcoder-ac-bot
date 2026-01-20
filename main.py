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
CIRCLE_COLORS = {
    "blue": 0x0000FF, "red": 0xFF0000, "orange": 0xFF8000,
    "yellow": 0xFFCC00, "green": 0x008000, "cyan": 0x00C0C0,
    "brown": 0x804000, "gray": 0x808080, "black": 0x000000
}

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
        
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open(SHEET_NAME)
        except Exception as e:
            print(f"⚠️ Google Sheets 連携エラー: {e}")

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
            for r in ws_config.get_all_records():
                self.news_config[str(r['GuildID'])] = int(r['ChannelID'])
            print("✅ Sheets復元完了")
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
        await self.tree.sync()

    # --- AC通知 ---
    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for key, info in list(self.user_data.items()):
                atcoder_id = info['atcoder_id']
                from_time = int(datetime.now().timestamp() - 600)
                url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={from_time}"
                async with session.get(url) as resp:
                    if resp.status != 200: continue
                    for sub in await resp.json():
                        if info.get('only_ac', True) and sub['result'] != 'AC': continue
                        sub_key = f"{info['guild_id']}_{atcoder_id}_{sub['id']}"
                        if sub_key in self.last_sub_ids: continue
                        self.last_sub_ids.add(sub_key)
                        await self.send_ac_notification(info, sub)

    async def send_ac_notification(self, info, sub):
        channel = self.get_channel(info['channel_id'])
        if not channel: return
        prob_id = sub['problem_id']
        diff = self.diff_map.get(prob_id, {}).get('difficulty', '不明')
        embed = discord.Embed(description=f"**[{self.problems_map.get(prob_id, prob_id)}](https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id})** | **[{sub['result']}]** | [📄提出](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})", color=self.get_diff_color(diff))
        embed.set_author(name=info['atcoder_id'])
        embed.add_field(name="", value=f"diff：{diff} | 言語：{sub['language']}")
        await channel.send(embed=embed)

    def get_diff_color(self, diff):
        if not isinstance(diff, int): return 0x000000
        colors = [0x808080, 0x804000, 0x008000, 0x00C0C0, 0x0000FF, 0xFFCC00, 0xFF8000, 0xFF0000]
        return colors[min(7, diff // 400)] if diff >= 0 else 0x000000

bot = AtCoderBot()

# --- コマンド群 (全維持) ---

@bot.tree.command(name="register", description="通知設定を登録・Sheets保存")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    bot.user_data[f"{interaction.guild_id}_{atcoder_id}"] = {"guild_id": interaction.guild_id, "discord_user_id": discord_user.id, "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac}
    bot.save_to_sheets()
    await interaction.response.send_message(f"✅ `{atcoder_id}` を登録しました。")

@bot.tree.command(name="delete", description="登録解除")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    key = f"{interaction.guild_id}_{atcoder_id}"
    if key in bot.user_data:
        del bot.user_data[key]
        bot.save_to_sheets()
        await interaction.response.send_message(f"🗑️ `{atcoder_id}` を解除しました。")
    else: await interaction.response.send_message("登録なし。", ephemeral=True)

@bot.tree.command(name="notice_set", description="定時ニュースの送信先を設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_to_sheets()
    await interaction.response.send_message(f"✅ ニュース送信先を {channel.mention} に設定。")

@bot.tree.command(name="notice_delete", description="定時ニュースの設定を解除")
async def notice_delete(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if guild_id in bot.news_config:
        del bot.news_config[guild_id]
        bot.save_to_sheets()
        await interaction.response.send_message("🗑️ ニュース設定解除。")
    else: await interaction.response.send_message("設定なし。", ephemeral=True)

@bot.tree.command(name="info", description="手動で情報を取得")
async def info(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        async with session.get("https://atcoder.jp/home") as resp:
            soup = BeautifulSoup(await resp.text(), 'html.parser')
            embeds = []
            table = soup.find('div', id='contest-table-upcoming')
            if table:
                for row in table.find_all('tr')[1:4]:
                    cols = row.find_all('td')
                    name_tag = cols[1].find('a')
                    embeds.append(discord.Embed(title=name_tag.text, url="https://atcoder.jp"+name_tag['href']).add_field(name="開始", value=cols[0].text))
            await interaction.followup.send(embeds=embeds if embeds else "予定なし")

@bot.tree.command(name="test_abc441", description="ABC441の通知テスト(WATCHING対応)")
async def test_abc441(interaction: discord.Interaction):
    await interaction.response.defer()
    target_id = next((v['atcoder_id'] for v in bot.user_data.values() if v['guild_id'] == interaction.guild_id and v['discord_user_id'] == interaction.user.id), "chokudai")
    contest_id, url = "abc441", "https://atcoder.jp/contests/abc441"
    start_dt = datetime.now(JST) + timedelta(seconds=15)
    pts_str, rating = "100-200-300-400-450-500-575", "~ 1999"
    
    e1 = discord.Embed(title="AtCoder Beginner Contest 441 (Promotion of Engineer Guild Fes)", url=url, color=get_rated_color(rating))
    e1.description = f"コンテストページ： {url}\n開始時刻： {start_dt.strftime('%Y-%m-%d %H:%M')}\nコンテスト時間： 100 分\nWriter： mechanicalpenciI, MMNMM, ynymxiaolongbao, evima\nTester： Nyaan, physics0523\nレーティング変化： {rating}\n配点： {pts_str}\n開始まで： <t:{int(start_dt.timestamp())}:R>"
    e1.set_footer(text=f"コンテスト時間：{start_dt.strftime('%Y年%m月%d日 %p %I:%M:%S').replace('AM','午前').replace('PM','午後')}")

    e2 = discord.Embed(title="AtCoder Beginner Contest 441", url=url, color=get_rated_color(rating))
    e2.description = f"🚀 **開始時刻となりました！**\n終了まで： <t:{int((start_dt + timedelta(minutes=100)).timestamp())}:R>\n\n**【配点内訳】**\n{pts_str}\n**合計　{sum(map(int, pts_str.split('-')))}点**\n\n📈 [順位表（{target_id}）]({url}/standings?watching={target_id}) | 📝 [自分の提出]({url}/submissions/me)"
    
    e3 = discord.Embed(title="AtCoder Beginner Contest 441", url=url, color=get_rated_color(rating), description="🏁 終了時刻となりました。お疲れ様でした！")
    
    await interaction.followup.send("🧪 テスト送信一式:")
    for e in [e1, e2, e3]: await interaction.channel.send(embed=e)

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_TOKEN"))
