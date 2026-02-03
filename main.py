import discord
from discord import app_commands
from discord.ext import tasks
import os, aiohttp, re, gspread
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

EMOJI_MAP = {
    "AC": "<:atcoder_bot_AC:1463065663429021917>",
    "WA": "<:atcoder_bot_WA:1463065707703959643>",
    "TLE": "<:atcoder_bot_TLE:1463065790256382086>",
    "RE": "<:atcoder_bot_RE:1463065747705172165>",
    "CE": "<:atcoder_bot_CE:1463065865561051228>",
    "MLE": "<:atcoder_bot_MLE:1463065831763349514>"
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
        self.sent_notifications = set()
        
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open(SHEET_NAME)
        except Exception as e: print(f"⚠️ Sheetsエラー: {e}")

    def save_to_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            ws_user.clear()
            # ヘッダーを書き込む
            ws_user.append_row(["GuildID", "AtCoderID", "DiscordID", "ChannelID", "OnlyAC", "LastSubID"])
            
            rows = []
            for key, v in self.user_data.items():
                # self.user_data の中身を1行ずつリストにする
                rows.append([
                    str(v['guild_id']), 
                    v['atcoder_id'], 
                    str(v['discord_user_id']), 
                    str(v['channel_id']), 
                    str(v['only_ac']), 
                    str(v.get('last_sub_id', 0))
                ])
            
            if rows:
                ws_user.append_rows(rows) # まとめてスプレッドシートへ
        except Exception as e:
            print(f"❌ 書き込み失敗: {e}")

    def load_from_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            for r in ws_user.get_all_records():
                # 「サーバーID_ユーザー名」で固有の鍵を作る
                gid = str(r['GuildID'])
                aid = r['AtCoderID']
                key = f"{gid}_{aid}"
                
                self.user_data[key] = {
                    "guild_id": int(gid),
                    "atcoder_id": aid,
                    "discord_user_id": int(r['DiscordID']),
                    "channel_id": int(r['ChannelID']),
                    "only_ac": str(r['OnlyAC']).lower() == 'true',
                    "last_sub_id": int(r.get('LastSubID', 0))
                }
        except Exception as e:
            print(f"❌ 読み込み失敗: {e}")
            
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

    @tasks.loop(minutes=3)
    async def check_submissions(self):
        # セッションをループの外で作成（効率化）
        async with aiohttp.ClientSession() as session:
            # 辞書のコピーに対してループを回す（実行中のサイズ変更エラー防止）
            for key in list(self.user_data.keys()):
                info = self.user_data[key]
                try:
                    await self.process_submissions(session, info, lookback_seconds=259200)
                except Exception as e:
                    print(f"⚠️ 提出確認エラー ({key}): {e}")

    async def process_submissions(self, session, info, lookback_seconds):
        atcoder_id = info['atcoder_id']
        guild_id = info['guild_id']
        
        # サーバーIDとAtCoderIDを組み合わせた「固有のキー」を作成
        # これにより、同じAtCoderIDでもサーバーが違えば別データとして扱われる
        key = f"{guild_id}_{atcoder_id}"
        
        # このサーバーでの「前回どこまで通知したか」を取得
        last_id = int(info.get('last_sub_id', 0))
        
        # Kenkoooo API から提出データを取得
        url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={int(datetime.now().timestamp() - lookback_seconds)}"
        
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    subs = await resp.json()
                    if not subs:
                        return

                    # 初回登録時（last_id=0）は、過去分を通知せず、最新IDのセットだけ行う（爆撃防止）
                    if last_id == 0:
                        latest_id = max(sub['id'] for sub in subs)
                        self.user_data[key]['last_sub_id'] = latest_id
                        self.save_to_sheets()
                        return

                    new_last_id = last_id
                    # 提出をIDの昇順（古い順）に並べてチェック
                    for sub in sorted(subs, key=lambda x: x['id']):
                        # すでに通知済みのIDならスキップ
                        if sub['id'] <= last_id:
                            continue
                        
                        # Only AC設定がONで、結果がACでないならスキップ
                        if info.get('only_ac', True) and sub['result'] != 'AC':
                            new_last_id = max(new_last_id, sub['id'])
                            continue
                        
                        # 通知を送信
                        await self.send_ac_notification(info, sub)
                        
                        # 送信した最新のIDを記録
                        new_last_id = max(new_last_id, sub['id'])
                    
                    # 最後にまとめてスプレッドシートを更新
                    if new_last_id > last_id:
                        self.user_data[key]['last_sub_id'] = new_last_id
                        self.save_to_sheets()
        except Exception as e:
            print(f"⚠️ process_submissions エラー ({key}): {e}")
            
    async def send_ac_notification(self, info, sub):
        channel = self.get_channel(info['channel_id'])
        if not channel: return
        prob_id, atcoder_id = sub['problem_id'], info['atcoder_id']
        prob_title = self.problems_map.get(prob_id, prob_id)
        difficulty = self.diff_map.get(prob_id, {}).get('difficulty')
        user = self.get_user(info['discord_user_id'])
        user_name = user.display_name if user else "Unknown"
        user_icon = user.display_avatar.url if user else None
        res = sub['result']
        emoji = EMOJI_MAP.get(res, "❓")
        def get_color(d):
            if d is None: return 0x808080
            colors = [(400, 0x808080), (800, 0x804000), (1200, 0x008000), (1600, 0x00C0C0), (2000, 0x0000FF), (2400, 0xFFFF00), (2800, 0xFF8000)]
            for limit, color in colors:
                if d < limit: return color
            return 0xFF0000
        embed = discord.Embed(title=prob_title, url=f"https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id}", color=get_color(difficulty))
        embed.set_author(name=f"{user_name}", icon_url=user_icon)
        exec_time = sub.get('execution_time') or 0
        desc = (f"user : [{atcoder_id}](https://atcoder.jp/users/{atcoder_id}) / result : {emoji} **[{res}]**\n"
                f"difficulty : {difficulty if difficulty is not None else '---'} / {exec_time}ms / score : {int(sub['point'])}\n"
                f"language : {sub['language']}\n\n"
                f"📄 [{atcoder_id}さんの提出を見る](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})\n"
                f"🔍 [解説を読む](https://atcoder.jp/contests/{sub['contest_id']}/editorial)")
        embed.description = desc
        dt = datetime.fromtimestamp(sub['epoch_second'], JST)
        embed.set_footer(text=f"提出時刻 : {dt.strftime('%b %d, %Y (%a) %H:%M:%S')}")
        await channel.send(embed=embed)

    async def fetch_recent_announcements(self, session, log_channel=None):
        results = {}
        try:
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                html = await resp.text()
            
            # 生のHTMLをデコードして、人間が見ている状態と同じにする
            import html as html_parser
            decoded = html_parser.unescape(html)
            soup = BeautifulSoup(decoded, 'html.parser')
            
            # 本質：この「告知パネル」自体を1つのコンテスト情報として独立して扱う
            posts = soup.find_all('div', class_='panel-default')
            
            for post in posts:
                # 告知の本文を取得
                body = post.find('div', class_='panel-body blog-post')
                if not body: continue
                
                # 1. コンテストURLを本文から抽出（紐付けの唯一の真実）
                # 例: https://atcoder.jp/contests/abc442
                link_tag = body.find('a', href=re.compile(r'https://atcoder\.jp/contests/[^" \n]+'))
                if not link_tag: continue
                c_url = link_tag['href'].split('?')[0].rstrip('/')
                
                # 2. 本文をテキスト化し、構造的にデータを抜き出す
                content = body.get_text("\n")
                
                info = {
                    "name": link_tag.get_text().strip(), # 告知内のコンテスト名
                    "writer": "不明",
                    "tester": "不明",
                    "points": "未発表",
                    "start_time": None
                }

                # 本質：提示されたソースの各行（- Writer: 等）を忠実にパース
                for line in content.split("\n"):
                    line = line.strip()
                    if "Writer：" in line:
                        info["writer"] = line.replace("- Writer：", "").strip()
                    elif "Tester：" in line:
                        info["tester"] = line.replace("- Tester：", "").strip()
                    elif "配点：" in line:
                        info["points"] = line.replace("- 配点：", "").strip()
                
                results[c_url] = info

            if log_channel:
                await log_channel.send(f"✅ 真の解析完了: {len(results)}件の告知を完全捕捉")
        except Exception as e:
            if log_channel: await log_channel.send(f"⚠️ 解析エラー: {e}")
        return results
        
    async def broadcast_contest(self, name, url, st, dur, rated, label, details, is_10min=False, is_start=False, is_end=False):
        # 終了通知(cend)の場合もユニークキーを作って二重送信防止
        key = f"{label}_{url}"
        if key in self.sent_notifications: return
        self.sent_notifications.add(key)
        embed = self.create_contest_embed(name, url, st, dur, rated, details, is_10min, is_start, is_end)
        for cid in self.news_config.values():
            channel = self.get_channel(cid)
            if channel: await channel.send(content=f"**{label}**", embed=embed)

    def create_contest_embed(self, name, url, st, dur, rated, details, is_10min=False, is_start=False, is_end=False):
        # 本質：どんなデータが来ても「文字」として成立させる
        def clean(text):
            if not text: return "不明"
            # 残っているHTMLタグを完全に排除
            res = re.sub(r'<[^>]*>', '', str(text)).strip()
            return res if res else "不明"

        writer = clean(details.get('writer'))
        tester = clean(details.get('tester'))
        points = clean(details.get('points'))

        embed = discord.Embed(title=name, url=url, color=get_rated_color(rated))
        
        # 本質：Embedの文字数制限と空文字禁止を回避
        if is_10min:
            embed.description = f"コンテストまで残り30分！\n👉 [参加登録]({url})\n配点： {points[:1000]}"
        elif is_start:
            embed.description = f"🚀 **開始！**\n\n**配点**： {points[:1000]}\n📈 [順位表]({url}/standings)"
        else:
            # 24時間前/本日開催通知
            embed.description = (f"開始： {st.strftime('%Y-%m-%d %H:%M')}\n"
                                 f"時間： {dur} 分\n"
                                 f"Writer： {writer[:500]}\n"
                                 f"Tester： {tester[:500]}\n"
                                 f"Rated： {rated}\n"
                                 f"配点： {points[:500]}\n"
                                 f"開始まで： <t:{int(st.timestamp())}:R>")
        
        embed.set_footer(text=f"AtCoder - {st.strftime('%Y/%m/%d')}")
        return embed

    async def check_immediate_announcement(self, channel_id):
        now = datetime.now(JST)
        channel = self.get_channel(channel_id)
        if not channel: return
        
        status_msg = await channel.send(f"⏳ 最終デプロイ確認中... ({now.strftime('%H:%M:%S')})")
        async with aiohttp.ClientSession() as session:
            recent_details = await self.fetch_recent_announcements(session, channel)
            
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                # 予定テーブル
                table = soup.find('div', id='contest-table-upcoming')
                if not table: return

                rows = table.find_all('tr')[1:]
                log_txt = "📊 **最終解析結果**\n```\n"
                found_any = False

                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) < 2: continue
                    
                    time_tag = row.find('time')
                    a_tag = cols[1].find('a')
                    if not time_tag or not a_tag: continue

                    c_url = "[https://atcoder.jp](https://atcoder.jp)" + a_tag['href'].split('?')[0].rstrip('/')
                    c_name = a_tag.text.strip()
                    
                    try:
                        st_dt = datetime.strptime(time_tag.text.strip(), '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                        diff = int((st_dt - now).total_seconds() / 60)

                        if 0 < diff <= 1440:
                            # 取得した本質データと合体
                            info = recent_details.get(c_url, {"writer":"確認中","tester":"確認中","points":"確認中"})
                            
                            # Embed送信で失敗してもループを止めないガード
                            try:
                                # 列の存在チェックを厳密に
                                duration = cols[2].text.strip() if len(cols) > 2 else "不明"
                                rated = cols[3].text.strip() if len(cols) > 3 else "不明"
                                
                                await self.broadcast_contest(c_name, c_url, st_dt, duration, rated, "⏰ 本日開催", info)
                                log_txt += f"・{c_name[:12]} | ✅ 送信成功\n"
                                found_any = True
                            except Exception as discord_e:
                                log_txt += f"・{c_name[:12]} | ❌ 400エラー: {str(discord_e)[:10]}\n"
                        else:
                            log_txt += f"・{c_name[:12]} | {diff}分前\n"
                    except: continue

                log_txt += "```"
                await status_msg.edit(content=log_txt)
                
    @tasks.loop(minutes=1)
    async def auto_contest_scheduler(self):
        now = datetime.now(JST).replace(second=0, microsecond=0)
        async with aiohttp.ClientSession() as session:
            recent_details = await self.fetch_recent_announcements(session)
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200: return
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                
                for table_id in ['contest-table-upcoming', 'contest-table-active']:
                    container = soup.find('div', id=table_id)
                    if not container: continue
                    table = container.find('table')
                    if not table: continue

                    for row in table.find_all('tr')[1:]:
                        cols = row.find_all('td')
                        if len(cols) < 4: continue
                        try:
                            time_tag = row.find('time')
                            time_str = time_tag.text.replace('\xa0', ' ').strip()
                            st_dt = datetime.strptime(re.sub(r'\(.*?\)', '', time_str).strip(), '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                            
                            dur = cols[2].text.strip()
                            h, m = map(int, dur.split(':'))
                            en_dt = st_dt + timedelta(hours=h, minutes=m)
                            
                            # 判定の安定化：roundを使用して微小なズレを許容
                            diff_st = round((st_dt - now).total_seconds() / 60)
                            diff_en = round((en_dt - now).total_seconds() / 60)
                            
                            name_tag = cols[1].find('a')
                            c_url = "https://atcoder.jp" + name_tag['href'].split('?')[0].rstrip('/')
                            details = recent_details.get(c_url, {"writer":"不明","tester":"不明","points":"不明"})
                            
                            # デバッグログ：必要に応じてコンソール等で確認
                            # print(f"Check: {name_tag.text} / diff_st: {diff_st}")

                            if diff_st == 1440: await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, cols[3].text.strip(), "⏰ 24時間前", details)
                            elif diff_st == 30: await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, cols[3].text.strip(), "⚠️ 30分前", details, is_10min=True)
                            elif diff_st == 0: await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, cols[3].text.strip(), "🚀 開始！", details, is_start=True)
                            elif diff_en == 0: await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, cols[3].text.strip(), "🏁 終了！", details, is_end=True)
                        except:
                            continue

bot = AtCoderBot()

@bot.tree.command(name="register", description="提出通知の登録")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    try: await interaction.response.defer()
    except: return
    info = {"guild_id": interaction.guild_id, "discord_user_id": discord_user.id, "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac, "last_sub_id": 0}
    bot.user_data[f"{interaction.guild_id}_{atcoder_id}"] = info
    bot.save_to_sheets()
    await interaction.followup.send(f"✅ `{atcoder_id}` 登録完了。")
    async with aiohttp.ClientSession() as session: await bot.process_submissions(session, info, lookback_seconds=86400)

@bot.tree.command(name="delete", description="提出通知の削除")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    try: await interaction.response.defer()
    except: return
    key = f"{interaction.guild_id}_{atcoder_id}"
    if key in bot.user_data:
        del bot.user_data[key]; bot.save_to_sheets()
        await interaction.followup.send(f"🗑️ `{atcoder_id}` 削除。")
    else: await interaction.followup.send("未登録です。")

@bot.tree.command(name="notice_set", description="告知チャンネル設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    try: await interaction.response.defer()
    except: return
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_to_sheets()
    await interaction.followup.send(f"✅ 告知先を {channel.mention} に設定。")
    await bot.check_immediate_announcement(channel.id)

@bot.tree.command(name="notice_delete", description="告知削除")
async def notice_delete(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except: return
    gid = str(interaction.guild_id)
    if gid in bot.news_config:
        del bot.news_config[gid]; bot.save_to_sheets()
        await interaction.followup.send("🗑️ 告知削除。")
    else: await interaction.followup.send("未設定。")

@bot.tree.command(name="preview", description="各種通知のプレビュー")
@app_commands.choices(type=[
    app_commands.Choice(name="提出通知", value="ac"),
    app_commands.Choice(name="24時間前", value="c24"),
    app_commands.Choice(name="30分前", value="c30"),
    app_commands.Choice(name="開始", value="cstart"),
    app_commands.Choice(name="終了", value="cend")
])
async def preview(interaction: discord.Interaction, type: str):
    try: await interaction.response.defer(ephemeral=True)
    except: return
    dummy_details = {"writer": "Staff", "tester": "Tester", "points": "100-200-300"}
    dummy_url = "https://atcoder.jp/contests/practice"
    dummy_st = datetime.now(JST)
    if type == "ac":
        await bot.send_ac_notification({'atcoder_id': 'atcoder', 'discord_user_id': interaction.user.id, 'channel_id': interaction.channel_id}, {'id': 0, 'problem_id': 'abc_a', 'contest_id': 'abc', 'result': 'AC', 'point': 100, 'language': 'Python', 'epoch_second': int(datetime.now().timestamp())})
    else:
        if type == "c24": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details)
        elif type == "c30": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details, is_10min=True)
        elif type == "cstart": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details, is_start=True)
        elif type == "cend": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details, is_end=True)
        await interaction.channel.send(content="**Preview**", embed=e)
    await interaction.followup.send("✅ 送信。")

if __name__ == "__main__":
    keep_alive(); bot.run(os.getenv("DISCORD_TOKEN"))
