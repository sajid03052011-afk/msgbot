import os, re, json, logging, threading, tempfile
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path
import pytz
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler, ContextTypes, filters)

# Paste a NEW BotFather token here before uploading/deploying.
# The previously shared token should be revoked and must not be reused.
TOKEN = 'PASTE_NEW_BOT_TOKEN_HERE'
OWNER_ID = 8984439807
DATA_FILE = Path('bot_data.json')
TZ = pytz.timezone('Asia/Kolkata')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('dispatcher')

DEFAULT_DB = {
    'channels': {},       # chat_id -> title
    'platforms': {},      # key -> {name, domains, channels, times, count, backup, enabled}
    'queue': [],          # items have platform, content, sent_to, attempts
    'history': [],
    'unknown_queue': [],
    'last_dispatch': {}
}

def load_db():
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
            for k, v in DEFAULT_DB.items(): data.setdefault(k, v.copy() if isinstance(v, (dict,list)) else v)
            return data
        except Exception as exc: log.exception('Could not load database: %s', exc)
    return json.loads(json.dumps(DEFAULT_DB))

db = load_db()

def save_db():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(DATA_FILE.parent), prefix='botdata_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(temp_name, DATA_FILE)
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)

def is_owner(update: Update):
    return bool(update.effective_user and OWNER_ID and update.effective_user.id == OWNER_ID)

MAIN_KB = ReplyKeyboardMarkup([
    ['➕ Add Channel', '📢 Channels'],
    ['➕ Add Platform', '⚙️ Manage Platforms'],
    ['📥 Queues', '⏰ Schedules'],
    ['📊 Statistics', '⏸ Pause / Resume'],
    ['🧭 Review Unknown', '🗑 Remove Channel'],
    ['🧹 Clear Queue', '❓ Help']
], resize_keyboard=True)

app_flask = Flask(__name__)
@app_flask.route('/')
@app_flask.route('/healthz')
def health(): return 'Dispatcher bot is running.'
def run_web(): app_flask.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))

def norm_domain(raw):
    raw = raw.strip().lower()
    if not raw: return ''
    if '://' not in raw: raw = 'https://' + raw
    host = urlparse(raw).netloc.lower().split('@')[-1].split(':')[0]
    return host[4:] if host.startswith('www.') else host

def extract_urls(text):
    if not text: return []
    found = re.findall(r'(?i)(?:https?://|www\.)[^\s<>]+', text)
    cleaned = []
    for u in found:
        u = u.rstrip('.,!?;:)]}')
        if u.startswith('www.'): u = 'https://' + u
        if u not in cleaned: cleaned.append(u)
    return cleaned

def identify_platform(text):
    urls = extract_urls(text)
    for key, p in db['platforms'].items():
        domains = [norm_domain(d) for d in p.get('domains', [])]
        for url in urls:
            host = norm_domain(url)
            if any(host == d or host.endswith('.' + d) for d in domains if d):
                return key, url
    return None, (urls[0] if urls else '')

def platform_key(name):
    base = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or 'platform'
    key, n = base, 2
    while key in db['platforms']:
        key, n = f'{base}_{n}', n + 1
    return key

def platform_keyboard(prefix='plat'):
    rows = [[InlineKeyboardButton(p['name'], callback_data=f'{prefix}:{k}')]
            for k,p in db['platforms'].items()]
    return InlineKeyboardMarkup(rows) if rows else None

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    context.user_data.clear()
    await update.message.reply_text('Cancelled.', reply_markup=MAIN_KB)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text('This is a private owner-only bot.')
        return
    context.user_data.clear()
    await update.message.reply_text('Welcome to Multi-Platform Channel Dispatcher.\nChoose an option:', reply_markup=MAIN_KB)

async def add_channel_help(update, context):
    me = await context.bot.get_me()
    url = f'https://t.me/{me.username}?startchannel=botstart&admin=post_messages+edit_messages'
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('➕ Select Channel and add bot', url=url)]])
    await update.message.reply_text('Add the bot as an administrator in each channel. It needs permission to post messages. The channel will be detected automatically.', reply_markup=kb)

async def on_bot_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    change = update.my_chat_member
    if not change or change.chat.type != 'channel' or change.new_chat_member.status != 'administrator': return
    if not OWNER_ID or change.from_user.id != OWNER_ID: return
    cid = str(change.chat.id)
    db['channels'][cid] = change.chat.title or cid
    save_db()
    try: await context.bot.send_message(OWNER_ID, f'✅ Channel added: {db["channels"][cid]}\nNow use Manage Platforms → choose a platform → Assign channels.')
    except Exception: pass

async def show_platforms(update):
    if not db['platforms']:
        await update.message.reply_text('No platforms yet. Tap ➕ Add Platform to create one.')
        return
    lines = []
    for k,p in db['platforms'].items():
        ch = ', '.join(db['channels'].get(c,c) for c in p.get('channels',[])) or 'Not assigned'
        status = 'ON' if p.get('enabled', True) else 'PAUSED'
        lines.append(f'• {p["name"]} [{status}]\n  Domains: {", ".join(p.get("domains",[]))}\n  Channels: {ch}\n  Schedule: {", ".join(p.get("times",[])) or "Not set"} | Batch: {p.get("count",1)}')
    await update.message.reply_text('\n\n'.join(lines), reply_markup=platform_keyboard('manage'))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    msg = update.message
    text = (msg.text or '').strip()
    state = context.user_data.get('state')
    if text == '➕ Add Channel': await add_channel_help(msg, context); return
    if text == '📢 Channels':
        if not db['channels']: await msg.reply_text('No channels connected yet.')
        else: await msg.reply_text('Connected channels:\n' + '\n'.join(f'• {v} ({k})' for k,v in db['channels'].items()))
        return
    if text == '➕ Add Platform':
        context.user_data.clear(); context.user_data['state'] = 'new_platform_name'
        await msg.reply_text('Send the platform name (example: Flezen).'); return
    if text == '⚙️ Manage Platforms': await show_platforms(msg); return
    if text == '🗑 Remove Channel':
        if not db['channels']:
            await msg.reply_text('No channels connected yet.'); return
        rows=[[InlineKeyboardButton('🗑 '+title,callback_data=f'removechan:{cid}')] for cid,title in db['channels'].items()]
        await msg.reply_text('Choose a channel to remove. Its platform mappings will also be cleared:', reply_markup=InlineKeyboardMarkup(rows)); return
    if text == '🧭 Review Unknown':
        if not db['unknown_queue']:
            await msg.reply_text('Unknown Queue is empty. Add the missing platform domain in Add Platform, then forward those posts again.'); return
        rows=[[InlineKeyboardButton(p['name'],callback_data=f'reassign:{k}')] for k,p in db['platforms'].items()]
        rows.append([InlineKeyboardButton('🗑 Clear Unknown Queue',callback_data='clear:unknown')])
        await msg.reply_text(f'{len(db["unknown_queue"])} post(s) have unmatched links. Select a platform to move all of them into its queue, or clear them.', reply_markup=InlineKeyboardMarkup(rows)); return
    if text == '📥 Queues':
        lines = [f'• {p["name"]}: {sum(1 for q in db["queue"] if q["platform"]==k)} pending' for k,p in db['platforms'].items()]
        lines.append(f'• Unknown / unconfigured: {len(db["unknown_queue"])} pending')
        await msg.reply_text('Queue status:\n' + ('\n'.join(lines) if lines else 'No platforms configured.')); return
    if text == '⏰ Schedules':
        if not db['platforms']: await msg.reply_text('Add a platform first.'); return
        await msg.reply_text('Choose a platform to edit its schedule:', reply_markup=platform_keyboard('schedule')); return
    if text == '📊 Statistics':
        lines = [f'Queued: {len(db["queue"])} | Unknown: {len(db["unknown_queue"])} | History: {len(db["history"])}']
        for k,p in db['platforms'].items():
            lines.append(f'{p["name"]}: {sum(1 for q in db["queue"] if q["platform"]==k)} pending, {len(p.get("channels",[]))} channel(s)')
        await msg.reply_text('\n'.join(lines)); return
    if text == '⏸ Pause / Resume':
        await msg.reply_text('Choose a platform:', reply_markup=platform_keyboard('pause')); return
    if text == '🧹 Clear Queue':
        await msg.reply_text('Choose which queue to clear:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(p['name'],callback_data=f'clear:{k}')] for k,p in db['platforms'].items()] + [[InlineKeyboardButton('Unknown queue',callback_data='clear:unknown')]])); return
    if text == '❓ Help':
        await msg.reply_text('Forward a post to me. I detect its platform from the URL domain and place it in that platform’s own queue. Each platform sends only to its assigned channel(s). Add platform domains under Add Platform. Unknown links go to Unknown Queue; they are never sent to another platform’s channel.'); return

    if state == 'new_platform_name':
        if not text or len(text)>40: await msg.reply_text('Please send a platform name up to 40 characters.'); return
        context.user_data['new_name'] = text; context.user_data['state'] = 'new_platform_domains'
        await msg.reply_text('Now send its domain(s), separated by commas. Example: example.com, links.example.net'); return
    if state == 'new_platform_domains':
        domains = list(dict.fromkeys(norm_domain(x) for x in text.split(',') if norm_domain(x)))
        if not domains: await msg.reply_text('I could not read a domain. Send one or more domains, e.g. example.com'); return
        name = context.user_data['new_name']; key = platform_key(name)
        db['platforms'][key] = {'name':name, 'domains':domains, 'channels':[], 'times':[], 'count':1, 'backup':'', 'enabled':True}
        save_db(); context.user_data.clear()
        await msg.reply_text(f'✅ Platform created: {name}\nDomains: {", ".join(domains)}\nNext: Manage Platforms → {name} → Assign channels and set schedule.', reply_markup=MAIN_KB); return
    if state and state.startswith('schedule_times:'):
        key = state.split(':',1)[1]
        vals = [x.strip() for x in text.split(',') if x.strip()]
        if any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in vals):
            await msg.reply_text('Invalid time. Use 24-hour format separated by commas, e.g. 09:00, 12:30, 18:00.'); return
        db['platforms'][key]['times'] = sorted(set(vals)); save_db(); context.user_data.clear()
        await msg.reply_text('✅ Schedule saved.', reply_markup=MAIN_KB); return
    if state and state.startswith('count:'):
        key = state.split(':',1)[1]
        if not text.isdigit() or not 1 <= int(text) <= 100: await msg.reply_text('Enter a number from 1 to 100.'); return
        db['platforms'][key]['count'] = int(text); save_db(); context.user_data.clear()
        await msg.reply_text('✅ Batch count saved.', reply_markup=MAIN_KB); return
    if state and state.startswith('backup:'):
        key = state.split(':',1)[1]
        if text and not extract_urls(text): await msg.reply_text('Please send a valid URL, or send /skip to leave it empty.'); return
        db['platforms'][key]['backup'] = text; save_db(); context.user_data.clear()
        await msg.reply_text('✅ Backup link saved.', reply_markup=MAIN_KB); return
    if text == '/skip': context.user_data.clear(); await msg.reply_text('Skipped.', reply_markup=MAIN_KB); return

async def forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    m = update.message
    if not (m.forward_origin or m.forward_from_chat or m.forward_from or m.forward_date): return
    raw = m.caption or m.text or ''
    key, link = identify_platform(raw)
    media_type, file_id = 'text', None
    if m.photo: media_type, file_id = 'photo', m.photo[-1].file_id
    elif m.video: media_type, file_id = 'video', m.video.file_id
    elif m.document: media_type, file_id = 'document', m.document.file_id
    elif m.animation: media_type, file_id = 'animation', m.animation.file_id
    elif m.audio: media_type, file_id = 'audio', m.audio.file_id
    item = {'id':f'{m.chat_id}_{m.message_id}_{int(datetime.now().timestamp())}', 'platform':key or 'unknown', 'type':media_type, 'file_id':file_id, 'raw_text':raw, 'link':link, 'sent_to':[], 'attempts':0, 'created_at':datetime.now(TZ).isoformat()}
    if key: db['queue'].append(item)
    else: db['unknown_queue'].append(item)
    save_db()
    q = len(db['queue']) if key else len(db['unknown_queue'])
    label = db['platforms'][key]['name'] if key else 'Unknown / unconfigured platform'
    await m.reply_text(f'📥 Added to {label}\nQueue position: #{q}\nDetected link: {link or "No URL found"}')

async def send_item(context, item, chat_id, platform):
    backup = platform.get('backup','').strip()
    link = item.get('link','')
    caption_parts = []
    if link: caption_parts += ['FULL VIDEO LINK 👇👇', link]
    if backup: caption_parts += ['NEW BACKUP CHANNEL 👇', backup]
    caption = '\n\n'.join(caption_parts) or item.get('raw_text','')
    typ, fid = item['type'], item.get('file_id')
    if typ == 'photo': await context.bot.send_photo(chat_id=int(chat_id), photo=fid, caption=caption[:1024] if caption else None)
    elif typ == 'video': await context.bot.send_video(chat_id=int(chat_id), video=fid, caption=caption[:1024] if caption else None)
    elif typ == 'document': await context.bot.send_document(chat_id=int(chat_id), document=fid, caption=caption[:1024] if caption else None)
    elif typ == 'animation': await context.bot.send_animation(chat_id=int(chat_id), animation=fid, caption=caption[:1024] if caption else None)
    elif typ == 'audio': await context.bot.send_audio(chat_id=int(chat_id), audio=fid, caption=caption[:1024] if caption else None)
    else: await context.bot.send_message(chat_id=int(chat_id), text=caption or item.get('raw_text','') or ' ')

async def dispatcher(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ); hm = now.strftime('%H:%M'); day_key = now.strftime('%Y-%m-%d')
    for key, p in list(db['platforms'].items()):
        if not p.get('enabled', True) or hm not in p.get('times',[]): continue
        stamp = f'{day_key} {hm}'
        if db['last_dispatch'].get(key) == stamp: continue
        batch = [x for x in db['queue'] if x.get('platform') == key][:p.get('count',1)]
        if not batch: continue
        db['last_dispatch'][key] = stamp; save_db()
        targets = p.get('channels',[])
        if not targets: log.warning('No channel mapping for platform %s', key); continue
        for item in batch:
            item['attempts'] = item.get('attempts',0) + 1
            for cid in targets:
                if cid in item.get('sent_to',[]): continue
                try:
                    await send_item(context, item, cid, p)
                    item.setdefault('sent_to',[]).append(cid)
                    db['history'].append({'platform':key,'channel':cid,'item_id':item['id'],'sent_at':datetime.now(TZ).isoformat()})
                except Exception as exc: log.error('Send failed platform=%s channel=%s: %s',key,cid,exc)
            save_db()
            if all(cid in item.get('sent_to',[]) for cid in targets):
                if item in db['queue']: db['queue'].remove(item)
        save_db()

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_owner(update): await q.edit_message_text('Owner access only.'); return
    data = q.data
    if data.startswith('manage:'):
        key=data.split(':',1)[1]; p=db['platforms'].get(key)
        if not p: return
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton('📢 Assign channels',callback_data=f'assign:{key}')],
            [InlineKeyboardButton('⏰ Set schedule',callback_data=f'settime:{key}')],
            [InlineKeyboardButton('🔢 Set batch count',callback_data=f'setcount:{key}')],
            [InlineKeyboardButton('🔗 Set backup link',callback_data=f'setbackup:{key}')],
            [InlineKeyboardButton('⏯ Pause / Resume',callback_data=f'pause:{key}')],
            [InlineKeyboardButton('🗑 Delete platform',callback_data=f'deleteplat:{key}')]
        ])
        await q.edit_message_text(f'{p["name"]}\nDomains: {", ".join(p["domains"])}\nAssigned channels: {len(p["channels"])}\nSchedule: {", ".join(p["times"]) or "Not set"}',reply_markup=kb)
    elif data.startswith('assign:'):
        key=data.split(':',1)[1]
        rows=[[InlineKeyboardButton(('✅ ' if cid in db['platforms'][key]['channels'] else '')+title,callback_data=f'togglechan:{key}:{cid}')] for cid,title in db['channels'].items()]
        rows.append([InlineKeyboardButton('💾 Done',callback_data=f'manage:{key}')])
        await q.edit_message_text('Select one or more destination channels:',reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    elif data.startswith('togglechan:'):
        _,key,cid=data.split(':',2); arr=db['platforms'][key]['channels']
        if cid in arr: arr.remove(cid)
        else: arr.append(cid)
        save_db(); rows=[[InlineKeyboardButton(('✅ ' if c in arr else '')+t,callback_data=f'togglechan:{key}:{c}')] for c,t in db['channels'].items()]
        rows.append([InlineKeyboardButton('💾 Done',callback_data=f'manage:{key}')])
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    elif data.startswith('settime:'):
        key=data.split(':',1)[1]; context.user_data['state']=f'schedule_times:{key}'
        await q.edit_message_text('Send schedule times separated by commas, 24-hour format. Example: 09:00, 12:30, 18:00\nSend /skip to cancel.')
    elif data.startswith('setcount:'):
        key=data.split(':',1)[1]; context.user_data['state']=f'count:{key}'
        await q.edit_message_text('How many posts from this platform should be sent per scheduled time? Enter 1–100.')
    elif data.startswith('setbackup:'):
        key=data.split(':',1)[1]; context.user_data['state']=f'backup:{key}'
        await q.edit_message_text('Send this platform’s backup URL, or /skip to cancel.')
    elif data.startswith('pause:'):
        key=data.split(':',1)[1]; p=db['platforms'][key]; p['enabled']=not p.get('enabled',True); save_db()
        await q.edit_message_text(f'{p["name"]}: {"Enabled" if p["enabled"] else "Paused"}')
    elif data.startswith('deleteplat:'):
        key=data.split(':',1)[1]; name=db['platforms'].pop(key,None)
        # Keep queued items safe by moving them to unknown queue.
        moved=[x for x in db['queue'] if x.get('platform')==key]
        db['queue']=[x for x in db['queue'] if x.get('platform')!=key]
        for x in moved: x['platform']='unknown'; db['unknown_queue'].append(x)
        save_db(); await q.edit_message_text(f'Platform deleted: {name["name"] if name else key}. Its pending posts were moved to Unknown Queue.')
    elif data.startswith('schedule:'):
        key=data.split(':',1)[1]; p=db['platforms'][key]
        await q.edit_message_text(f'{p["name"]} current schedule: {", ".join(p["times"]) or "Not set"}\nUse Manage Platforms to edit it.')
    elif data.startswith('pause:'):
        key=data.split(':',1)[1]; p=db['platforms'][key]; p['enabled']=not p.get('enabled',True); save_db(); await q.edit_message_text('Updated.')
    elif data.startswith('removechan:'):
        cid=data.split(':',1)[1]; title=db['channels'].pop(cid,None)
        for p in db['platforms'].values():
            p['channels']=[x for x in p.get('channels',[]) if x != cid]
        save_db(); await q.edit_message_text(f'Removed channel: {title or cid}. Platform mappings updated.')
    elif data.startswith('reassign:'):
        key=data.split(':',1)[1]; moved=len(db['unknown_queue'])
        for item in db['unknown_queue']:
            item['platform']=key; item['sent_to']=[]
            db['queue'].append(item)
        db['unknown_queue']=[]; save_db()
        await q.edit_message_text(f'Moved {moved} post(s) to {db["platforms"][key]["name"]} queue. They will go only to that platform’s assigned channels.')
    elif data.startswith('clear:'):
        key=data.split(':',1)[1]
        if key=='unknown': n=len(db['unknown_queue']); db['unknown_queue']=[]
        else: n=sum(1 for x in db['queue'] if x.get('platform')==key); db['queue']=[x for x in db['queue'] if x.get('platform')!=key]
        save_db(); await q.edit_message_text(f'Cleared {n} queued post(s).')

async def error_handler(update, context): log.exception('Update error: %s', context.error)

def main():
    if not TOKEN or TOKEN == '8863781796:AAF7LQwOQJkWYSNtqaz1P-pZzVNIxfzCGUs':
        raise RuntimeError('Open msgbot.py and paste your NEW BotFather token into TOKEN first.')
    if not OWNER_ID:8984439807
        raise RuntimeError('Set OWNER_ID in msgbot.py before starting the bot.')
    threading.Thread(target=run_web,daemon=True).start()
    application=ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler('start',start))
    application.add_handler(CommandHandler('skip',skip_command))
    application.add_handler(ChatMemberHandler(on_bot_membership,ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.FORWARDED & (filters.PHOTO|filters.VIDEO|filters.Document.ALL|filters.ANIMATION|filters.AUDIO|filters.TEXT),forwarded))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    if application.job_queue: application.job_queue.run_repeating(dispatcher,interval=15,first=10)
    application.add_error_handler(error_handler)
    application.run_polling(drop_pending_updates=False)

if __name__=='__main__': main()
