//! ttrpg-sidecar: Discord voice receive for ttrpg-scribe.
//!
//! Joins one voice channel (guild/channel matched by name substring) and
//! streams per-user decoded PCM plus display names to stdout as framed
//! binary for the Python transcriber. Stdout is binary — all logs go to
//! stderr. Exits when stdin closes (parent gone) or on Ctrl+C.
//!
//! Usage: ttrpg-sidecar --guild "My Server" --channel "Game Night"
//! Token via DISCORD_TOKEN env (never argv — argv leaks into process lists).

use std::{
    collections::HashMap,
    env,
    io::Write,
    sync::Arc,
    time::Duration,
};

use dashmap::DashMap;
use serenity::{
    async_trait,
    client::{Client, Context, EventHandler},
    model::{
        gateway::Ready,
        id::{ChannelId, GuildId, UserId},
        prelude::ChannelType,
    },
    prelude::GatewayIntents,
};
use songbird::{
    driver::{DecodeConfig, DecodeMode},
    model::payload::Speaking,
    Config, CoreEvent, Event, EventContext, EventHandler as VoiceEventHandler, SerenityInit,
};

/// One frame on stdout: tag + uid + len + body, all little-endian.
fn emit(tag: u8, uid: u64, body: &[u8]) {
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(&[tag]);
    let _ = out.write_all(&uid.to_le_bytes());
    let _ = out.write_all(&(body.len() as u32).to_le_bytes());
    let _ = out.write_all(body);
    let _ = out.flush();
}

fn emit_name(uid: u64, name: &str) {
    emit(b'N', uid, name.as_bytes());
}

fn emit_audio(uid: u64, pcm: &[i16]) {
    // i16 LE bytes without an extra allocation per sample.
    let mut buf = Vec::with_capacity(pcm.len() * 2);
    for s in pcm {
        buf.extend_from_slice(&s.to_le_bytes());
    }
    emit(b'A', uid, &buf);
}

#[derive(Clone)]
struct Receiver {
    inner: Arc<InnerReceiver>,
}

struct InnerReceiver {
    ssrc_to_uid: DashMap<u32, u64>,
    /// Logged once: decoded-audio shape of the first tick, so the Python
    /// side's 48 kHz stereo assumption is verified against reality.
    logged_format: std::sync::atomic::AtomicBool,
}

impl Receiver {
    fn new() -> Self {
        Self {
            inner: Arc::new(InnerReceiver {
                ssrc_to_uid: DashMap::new(),
                logged_format: std::sync::atomic::AtomicBool::new(false),
            }),
        }
    }
}

fn uid_of(user_id: &songbird::model::id::UserId) -> u64 {
    user_id.to_string().parse().unwrap_or(0)
}

#[async_trait]
impl VoiceEventHandler for Receiver {
    async fn act(&self, ctx: &EventContext<'_>) -> Option<Event> {
        use EventContext as Ctx;
        match ctx {
            Ctx::SpeakingStateUpdate(Speaking {
                ssrc, user_id, ..
            }) => {
                if let Some(uid) = user_id {
                    self.inner.ssrc_to_uid.insert(*ssrc, uid_of(uid));
                }
            }
            Ctx::VoiceTick(tick) => {
                for (ssrc, data) in &tick.speaking {
                    let uid = match self.inner.ssrc_to_uid.get(ssrc) {
                        Some(id) => *id,
                        None => continue,
                    };
                    if let Some(pcm) = data.decoded_voice.as_ref() {
                        if !self
                            .inner
                            .logged_format
                            .swap(true, std::sync::atomic::Ordering::SeqCst)
                        {
                            eprintln!(
                                "[sidecar] first voice tick: {} speakers, {} samples/user",
                                tick.speaking.len(),
                                pcm.len()
                            );
                        }
                        emit_audio(uid, pcm);
                    }
                }
            }
            _ => {}
        }
        None
    }
}

struct Handler {
    guild_want: String,
    channel_want: String,
}

fn contains(haystack: &str, needle: &str) -> bool {
    haystack.to_lowercase().contains(&needle.to_lowercase())
}

/// All cached guilds as (id, name), for lookup + diagnostics.
fn cached_guilds(ctx: &Context) -> Vec<(GuildId, String)> {
    ctx.cache
        .guilds()
        .iter()
        .filter_map(|id| ctx.cache.guild(*id).map(|g| (*id, g.name.clone())))
        .collect()
}

/// Occupant user ids + display names for one voice channel, from cache.
/// Skips our own user id (we never speak).
fn occupant_names(
    ctx: &Context,
    guild_id: GuildId,
    channel_id: ChannelId,
    skip: UserId,
) -> HashMap<u64, String> {
    let mut out = HashMap::new();
    let Some(guild) = ctx.cache.guild(guild_id) else {
        return out;
    };
    for (uid, vs) in &guild.voice_states {
        if vs.channel_id != Some(channel_id) || *uid == skip {
            continue;
        }
        let name = match guild.members.get(uid) {
            Some(m) => m
                .nick
                .clone()
                .or_else(|| m.user.global_name.clone())
                .unwrap_or_else(|| m.user.name.clone()),
            None => vs.user_id.to_string(),
        };
        if let Ok(id) = uid.to_string().parse::<u64>() {
            out.insert(id, name);
        }
    }
    out
}

#[async_trait]
impl EventHandler for Handler {
    async fn ready(&self, ctx: Context, ready: Ready) {
        eprintln!("[sidecar] logged in as {}", ready.user.name);

        // Guild cache can lag `ready` (lazy guilds); poll briefly, then
        // report exactly what we see so name mismatches are obvious.
        let mut guilds = cached_guilds(&ctx);
        for _ in 0..10 {
            if !guilds.is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
            guilds = cached_guilds(&ctx);
        }
        let guild_id: GuildId = match guilds
            .iter()
            .find(|(_, name)| self.guild_want.is_empty() || contains(name, &self.guild_want))
            .map(|(id, _)| *id)
        {
            Some(id) => id,
            None => {
                eprintln!(
                    "[sidecar] no guild matching {:?}; cached guilds: {:?}",
                    self.guild_want,
                    guilds.iter().map(|(_, n)| n).collect::<Vec<_>>()
                );
                std::process::exit(2);
            }
        };

        let channel_id: ChannelId = {
            let guild = ctx.cache.guild(guild_id).expect("guild vanished");
            let voice: Vec<(ChannelId, String)> = guild
                .channels
                .values()
                .filter(|c| c.kind == ChannelType::Voice)
                .map(|c| (c.id, c.name.clone()))
                .collect();
            match voice
                .iter()
                .find(|(_, name)| self.channel_want.is_empty() || contains(name, &self.channel_want))
                .map(|(id, _)| *id)
            {
                Some(id) => id,
                None => {
                    eprintln!(
                        "[sidecar] no voice channel matching {:?}; channels: {:?}",
                        self.channel_want,
                        voice.iter().map(|(_, n)| n).collect::<Vec<_>>()
                    );
                    std::process::exit(2);
                }
            }
        };

        for (uid, name) in occupant_names(&ctx, guild_id, channel_id, ready.user.id) {
            emit_name(uid, &name);
        }

        let manager = songbird::get(&ctx)
            .await
            .expect("songbird registered")
            .clone();

        {
            let call = manager.get_or_insert(guild_id);
            let mut handler = call.lock().await;
            // One shared receiver: the SSRC map (speaking events) and the
            // audio ticks must see the same state.
            let rx = Receiver::new();
            handler.add_global_event(CoreEvent::SpeakingStateUpdate.into(), rx.clone());
            handler.add_global_event(CoreEvent::VoiceTick.into(), rx);
        }

        // Re-snapshot names: late joiners / nickname changes.
        let ctx2 = ctx.clone();
        let self_id = ready.user.id;
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(30)).await;
                for (uid, name) in occupant_names(&ctx2, guild_id, channel_id, self_id) {
                    emit_name(uid, &name);
                }
            }
        });

        if let Err(why) = manager.join(guild_id, channel_id).await {
            eprintln!("[sidecar] join failed: {:?}", why);
            std::process::exit(2);
        }
        eprintln!("[sidecar] joined voice, streaming pcm to stdout");
    }
}

fn arg_value(name: &str) -> String {
    let mut it = env::args().skip(1);
    while let Some(a) = it.next() {
        if a == name {
            return it.next().unwrap_or_default();
        }
        if let Some(v) = a.strip_prefix(&format!("{name}=")) {
            return v.to_string();
        }
    }
    String::new()
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .init();

    // Die with the parent: Python holds stdin open and closes it on shutdown.
    std::thread::spawn(|| {
        let mut buf = [0u8; 64];
        use std::io::Read;
        while std::io::stdin().read(&mut buf).unwrap_or(0) > 0 {}
        std::process::exit(0);
    });

    let token = env::var("DISCORD_TOKEN").unwrap_or_default();
    if token.is_empty() {
        eprintln!("[sidecar] DISCORD_TOKEN env is required");
        std::process::exit(2);
    }
    let handler = Handler {
        guild_want: arg_value("--guild"),
        channel_want: arg_value("--channel"),
    };

    let intents = GatewayIntents::non_privileged() | GatewayIntents::GUILD_MEMBERS;
    let songbird_config =
        Config::default().decode_mode(DecodeMode::Decode(DecodeConfig::default()));

    let mut client = Client::builder(&token, intents)
        .event_handler(handler)
        .register_songbird_from_config(songbird_config)
        .await
        .expect("client build");

    if let Err(why) = client.start().await {
        eprintln!("[sidecar] client ended: {:?}", why);
    }
}
