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

# --- 設定・定数 ---
USER_DATA_FILE = "users.json"
NEWS_CONFIG_FILE = "news_config.json"
JST = timezone(timedelta(hours=9))
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
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://kenkoooo.com/atcoder/resources/problems.json") as r:
                    if r.status == 200:
                        p = await r.json()
                        self.problems_map = {x['id']: x['title'] for x in p}
                async with session.get("https://kenkoooo.com/atcoder/resources/problem-models.json") as r:
                    if r.status == 200:
                        self.diff_map = await r.json()
        except: print("API resources load failed.")
        
        self.check_submissions.start()
        await self.tree.sync()

    # --- AC通知ロジック ---
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

# --- コマンド群 ---

@bot.tree.command(name="register", description="通知設定を登録します")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    unique_key = f"{interaction.guild_id}_{atcoder_id}"
    bot.user_data[unique_key] = {
        "guild_id": interaction.guild_id, "discord_user_id": discord_user.id,
        "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac,
        "registered_at": datetime.now().timestamp()
    }
    bot.save_json(bot.user_data, USER_DATA_FILE)
    await interaction.response.send_message(f"✅ `{atcoder_id}` を登録しました。")

@bot.tree.command(name="delete", description="登録を解除します")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    unique_key = f"{interaction.guild_id}_{atcoder_id}"
    if unique_key in bot.user_data:
        del bot.user_data[unique_key]
        bot.save_json(bot.user_data, USER_DATA_FILE)
        await interaction.response.send_message(f"🗑️ `{atcoder_id}` を解除しました。")
    else:
        await interaction.response.send_message("❓ 登録が見つかりません。", ephemeral=True)

@bot.tree.command(name="notice_set", description="定時ニュースの送信先を設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_json(bot.news_config, NEWS_CONFIG_FILE)
    await interaction.response.send_message(f"✅ ニュース送信先を {channel.mention} に設定。")

@bot.tree.command(name="notice_delete", description="定時ニュースの設定を解除")
async def notice_delete(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if guild_id in bot.news_config:
        del bot.news_config[guild_id]
        bot.save_json(bot.news_config, NEWS_CONFIG_FILE)
        await interaction.response.send_message("🗑️ ニュース設定を解除しました。")
    else:
        await interaction.response.send_message("❓ 設定されていません。", ephemeral=True)

@bot.tree.command(name="info", description="手動でAtCoder情報を表示します")
async def info(interaction: discord.Interaction):
    await interaction.response.defer()
    url = "https://atcoder.jp/home"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            soup = BeautifulSoup(await resp.text(), 'html.parser')
            embeds = []
            table = soup.find('div', id='contest-table-upcoming')
            if table:
                for row in table.find_all('tr')[1:4]:
                    cols = row.find_all('td')
                    time_str, name_tag = cols[0].text, cols[1].find('a')
                    img = cols[1].find('img')
                    color = CIRCLE_COLORS["black"]
                    if img:
                        for c in CIRCLE_COLORS:
                            if c in img['src']: color = CIRCLE_COLORS[c]; break
                    embeds.append(discord.Embed(title=name_tag.text, url="https://atcoder.jp"+name_tag['href'], color=color).add_field(name="開始時刻", value=time_str))
            
            important = soup.find('div', id='home-important-notices')
            if important:
                n_list = [li.text.strip() for li in important.find_all('li')[:5]]
                embeds.append(discord.Embed(title="✅ 重要な告知", description="\n".join([f"• {n}" for n in n_list]), color=0x008000))
            
            await interaction.followup.send(embeds=embeds)

@bot.tree.command(name="test_abc441", description="ABC441の通知テスト（何度でも実行可能）")
async def test_abc441(interaction: discord.Interaction):
    await interaction.response.defer()
    
    contest_id = "abc441"
    short_name = "AtCoder Beginner Contest 441"
    contest_url = f"https://atcoder.jp/contests/{contest_id}"
    start_dt = datetime.now(JST) + timedelta(seconds=15)
    pts_str = "100-200-300-400-450-500-575"
    rating = "~ 1999"
    color = get_rated_color(rating)
    
    # 告知
    e1 = discord.Embed(title=short_name + " (Test Edition)", url=contest_url, color=color)
    e1.description = f"開始まで： <t:{int(start_dt.timestamp())}:R>\n配点： {pts_str}"
    
    # 開始
    end_dt = start_dt + timedelta(minutes=100)
    pts = pts_str.split('-')
    labels = ["A","B","C","D","E","F","G"]
    pt_txt = "".join([f"**{labels[i]}** {pts[i]}点 " + ("\n" if (i+1)%4==0 else "") for i in range(len(pts))])
    e2 = discord.Embed(title=short_name, url=contest_url, color=color)
    e2.description = f"🚀 **開始時刻となりました！**\n終了まで： <t:{int(end_dt.timestamp())}:R>\n\n**【配点内訳】**\n{pt_txt}\n\n📈 [順位表]({contest_url}/standings) | 📝 [自分の提出]({contest_url}/submissions/me)"

    # 終了
    e3 = discord.Embed(title=short_name, url=contest_url, color=color, description="🏁 **終了時刻となりました。お疲れ様でした！**")

    await interaction.followup.send("🧪 テスト送信一式:")
    await interaction.channel.send(embed=e1)
    await interaction.channel.send(embed=e2)
    await interaction.channel.send(embed=e3)

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_TOKEN"))
