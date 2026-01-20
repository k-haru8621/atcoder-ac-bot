import discord
from discord import app_commands
from discord.ext import tasks
import os, json, requests, aiohttp, re
from datetime import datetime, timedelta, timezone, time
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
USER_DATA_FILE = "users.json"
NEWS_CONFIG_FILE = "news_config.json"
JST = timezone(timedelta(hours=9))

def get_rated_color(rating_str):
    """Rated上限からEmbedの色を決定する"""
    if "All" in rating_str: return 0xFF0000 # AGC/AHC 赤
    match = re.search(r'(\d+)', rating_str)
    if not match: return 0x000000 # 黒
    val = int(match.group(1))
    if val < 1200: return 0x008000 # 緑
    if val < 2000: return 0x0000FF # 青
    if val < 2800: return 0xFF8000 # 橙
    return 0xFF0000 # 赤

class AtCoderBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_data = self.load_json(USER_DATA_FILE)
        self.news_config = self.load_json(NEWS_CONFIG_FILE)
        self.problems_map = {}
        self.diff_map = {}
        self.last_sub_ids = set()

    def load_json(self, path):
        if os.path.exists(path):
            try:
                with open(path, "r") as f: return json.load(f)
            except: return {}
        return {}

    def save_json(self, data, path):
        with open(path, "w") as f: json.dump(data, f)

    async def setup_hook(self):
        # 起動時にリソース読み込み
        try:
            p = requests.get("https://kenkoooo.com/atcoder/resources/problems.json").json()
            self.problems_map = {x['id']: x['title'] for x in p}
            d = requests.get("https://kenkoooo.com/atcoder/resources/problem-models.json").json()
            self.diff_map = d
        except: print("API resources load failed.")
        
        self.check_submissions.start()
        await self.tree.sync()

    # --- AC通知ロジック (省略せず統合) ---
    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for key, info in list(self.user_data.items()):
                atcoder_id = info['atcoder_id']
                from_time = int(datetime.now().timestamp() - 600)
                url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={from_time}"
                async with session.get(url) as resp:
                    if resp.status != 200: continue
                    subs = await resp.json()
                    for sub in subs:
                        if info.get('only_ac', True) and sub['result'] != 'AC': continue
                        sub_key = f"{info['guild_id']}_{atcoder_id}_{sub['id']}"
                        if sub_key in self.last_sub_ids: continue
                        self.last_sub_ids.add(sub_key)
                        await self.send_ac_notification(info, sub)

    async def send_ac_notification(self, info, sub):
        channel = self.get_channel(info['channel_id'])
        if not channel: return
        prob_id = sub['problem_id']
        prob_title = self.problems_map.get(prob_id, prob_id)
        diff_val = self.diff_map.get(prob_id, {}).get('difficulty', '不明')
        color = 0xFFFFFF if sub['result'] != 'AC' else self.get_diff_color(diff_val)
        embed = discord.Embed(description=f"**[{prob_title}](https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id})** | **[{sub['result']}]** | [📄提出](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})", color=color)
        embed.set_author(name=f"{info['atcoder_id']}")
        embed.add_field(name="", value=f"diff：{diff_val} | 言語：{sub['language']}", inline=False)
        await channel.send(embed=embed)

    def get_diff_color(self, diff):
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

# --- コマンド類 ---

@bot.tree.command(name="register", description="AC通知を登録します")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    unique_key = f"{interaction.guild_id}_{atcoder_id}"
    bot.user_data[unique_key] = {
        "guild_id": interaction.guild_id, "discord_user_id": discord_user.id,
        "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac
    }
    bot.save_json(bot.user_data, USER_DATA_FILE)
    await interaction.response.send_message(f"✅ `{atcoder_id}` を登録しました。")

@bot.tree.command(name="notice_set", description="ニュース送信先を設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_json(bot.news_config, NEWS_CONFIG_FILE)
    await interaction.response.send_message(f"✅ 送信先を {channel.mention} に設定。")

# --- テスト用コマンド (告知・開始・終了を一斉送信) ---
@bot.tree.command(name="test_abc441", description="ABC441の通知テストを一斉送信します")
async def test_abc441(interaction: discord.Interaction):
    await interaction.response.defer()
    
    # テストデータ
    contest_id = "abc441"
    full_name = "AtCoder Beginner Contest 441 (Promotion of Engineer Guild Fes)"
    short_name = "AtCoder Beginner Contest 441"
    start_dt = datetime.now(JST) + timedelta(seconds=10) # 10秒後開始と想定
    duration = 100
    pts_str = "100-200-300-400-450-500-575"
    rating = "~ 1999"
    color = get_rated_color(rating)
    
    # 1. 告知 Embed
    unix_start = int(start_dt.timestamp())
    e1 = discord.Embed(title=full_name, url=f"https://atcoder.jp/contests/{contest_id}", color=color)
    e1.description = (
        f"コンテストページ： https://atcoder.jp/contests/{contest_id}\n"
        f"開始時刻： {start_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"コンテスト時間： {duration} 分\n"
        f"Writer： mechanicalpenciI, MMNMM, ynymxiaolongbao, evima\n"
        f"Tester： Nyaan, physics0523\n"
        f"レーティング変化： {rating}\n"
        f"配点： {pts_str}\n"
        f"コンテスト開始まで： <t:{unix_start}:R>"
    )
    e1.set_footer(text=f"コンテスト時間：{start_dt.strftime('%Y年%m月%d日 %p %I:%M:%S').replace('AM','午前').replace('PM','午後')}")

    # 2. 開始 Embed
    end_dt = start_dt + timedelta(minutes=duration)
    unix_end = int(end_dt.timestamp())
    pts = pts_str.split('-')
    labels = ["A問題","B問題","C問題","D問題","E問題","F問題","G問題"]
    point_text = ""
    total = 0
    for i, p in enumerate(pts):
        point_text += f"{labels[i]} {p}点　"
        total += int(p)
        if (i+1) % 2 == 0: point_text += "\n"
    
    e2 = discord.Embed(title=short_name, color=color)
    e2.description = f"開始時刻となりました。残り時間は <t:{unix_end}:R> です。\n\n**配点**\n{point_text}\n**合計 {total}点**"

    # 3. 終了 Embed
    e3 = discord.Embed(title=short_name, description="終了時刻となりました。お疲れ様でした。", color=color)

    await interaction.followup.send("🧪 テスト送信を開始します（本来は別々のタイミングで送られます）")
    await interaction.channel.send("【テスト1: 告知】", embed=e1)
    await interaction.channel.send("【テスト2: 開始】", embed=e2)
    await interaction.channel.send("【テスト3: 終了】", embed=e3)

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_TOKEN"))
