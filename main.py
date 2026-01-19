import discord
from discord import app_commands
from discord.ext import tasks
import os
import json
import requests
import aiohttp
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

# --- Flask Server (Renderのスリープ対策用) ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- AtCoder Bot ---
DATA_FILE = "users.json"

class AtCoderBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # ユーザー情報取得用
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_data = self.load_data()
        self.problems_map = {}
        self.diff_map = {}
        self.last_sub_ids = {} # メモリ上で直近の通知済みIDを管理

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    return json.load(f)
            except: return {}
        return {}

    def save_data(self):
        with open(DATA_FILE, "w") as f:
            json.dump(self.user_data, f)

    def load_atcoder_resources(self):
        try:
            p_res = requests.get("https://kenkoooo.com/atcoder/resources/problems.json").json()
            self.problems_map = {p['id']: p['title'] for p in p_res}
            d_res = requests.get("https://kenkoooo.com/atcoder/resources/problem-models.json").json()
            self.diff_map = d_res
            print("AtCoder resources loaded.")
        except Exception as e:
            print(f"Resource load error: {e}")

    async def setup_hook(self):
        self.load_atcoder_resources()
        self.check_submissions.start()
        await self.tree.sync()

    def get_difficulty_color(self, diff):
        if not isinstance(diff, int): return 0x000000
        if diff < 400: return 0x808080 # 灰
        if diff < 800: return 0x804000 # 茶
        if diff < 1200: return 0x008000 # 緑
        if diff < 1600: return 0x00C0C0 # 水
        if diff < 2000: return 0x0000FF # 青
        if diff < 2400: return 0xFFCC00 # 黄
        if diff < 2800: return 0xFF8000 # 橙
        return 0xFF0000 # 赤

    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for atcoder_id, info in self.user_data.items():
                channel_id = info['channel_id']
                discord_user_id = info['discord_user_id']
                
                # 直近10分間の提出を確認
                now = int(datetime.now().timestamp())
                url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={now - 600}"
                
                async with session.get(url) as resp:
                    if resp.status != 200: continue
                    subs = await resp.json()
                    
                    for sub in subs:
                        # ACのみ通知、かつ重複通知を防止
                        if sub['result'] == 'AC':
                            sub_key = f"{atcoder_id}_{sub['id']}"
                            if sub_key in self.last_sub_ids: continue
                            self.last_sub_ids[sub_key] = True

                            await self.send_notification(channel_id, atcoder_id, discord_user_id, sub)

    async def send_notification(self, channel_id, atcoder_id, discord_user_id, sub):
        channel = self.get_channel(channel_id)
        if not channel: return

        # ユーザー情報の取得（キャッシュにあればそれを使う）
        member = channel.guild.get_member(discord_user_id)
        user_name = member.display_name if member else "Unknown"
        avatar_url = member.display_avatar.url if member else None

        # 問題・diff情報
        prob_id = sub['problem_id']
        prob_title = self.problems_map.get(prob_id, prob_id)
        diff_info = self.diff_map.get(prob_id, {})
        diff_val = diff_info.get('difficulty', '不明')
        color = self.get_difficulty_color(diff_val)

        # 時間の整形 (JST)
        dt_jst = datetime.fromtimestamp(sub['epoch_second'], timezone(timedelta(hours=9)))
        time_str = dt_jst.strftime('%Y年%m月%d日 %p %I:%M:%S').replace('AM', '午前').replace('PM', '午後')

        # Embed作成
        embed = discord.Embed(
            description=f"**[{prob_title}](https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id})** | **[{sub['result']}]** | [📄提出](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})",
            color=color
        )
        if avatar_url:
            embed.set_author(name=f"{user_name} / {atcoder_id}", icon_url=avatar_url)
        else:
            embed.set_author(name=f"{user_name} / {atcoder_id}")

        embed.add_field(
            name="",
            value=f"diff：{diff_val} | 言語：{sub['language']} | 実行時間：{sub['execution_time']} ms",
            inline=False
        )
        embed.add_field(name="---", value=f"コンテスト: {sub['contest_id'].upper()}", inline=False)
        embed.set_footer(text=f"提出時間：{time_str}")

        await channel.send(embed=embed)

bot = AtCoderBot()

@bot.tree.command(name="register", description="AtCoder IDをこのチャンネルに登録します")
async def register(interaction: discord.Interaction, atcoder_id: str):
    bot.user_data[atcoder_id] = {
        "channel_id": interaction.channel_id,
        "discord_user_id": interaction.user.id
    }
    bot.save_data()
    await interaction.response.send_message(f"ID: `{atcoder_id}` を登録しました！ACするとこのチャンネルに通知します。")

@bot.tree.command(name="delete", description="登録を解除します")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    if atcoder_id in bot.user_data:
        del bot.user_data[atcoder_id]
        bot.save_data()
        await interaction.response.send_message(f"ID: `{atcoder_id}` の登録を解除しました。")
    else:
        await interaction.response.send_message("そのIDは登録されていません。")

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if token:
        bot.run(token)
    else:
        print("Error: DISCORD_TOKEN not found.")
