import logging
import sqlite3
import signal
import sys
from datetime import datetime, timedelta
from contextlib import contextmanager
from threading import Lock
import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
import os
import time
import asyncio
import aiohttp
from dotenv import load_dotenv
from collections import deque
import json
from prometheus_client import Counter, Histogram, Gauge, start_http_server
from typing import Optional, List, Dict, Any

# Database configuration
DATA_DIR = "/app/data"
DB_FILE = os.path.join(DATA_DIR, "bot_data.db")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Log database information for debugging
logger.info(f"Database file: {DB_FILE}")
logger.info(f"Data directory: {DATA_DIR}")
logger.info(f"Log file: {LOG_FILE}")

# Test write permissions
try:
    test_file = os.path.join(DATA_DIR, "write_test.tmp")
    with open(test_file, 'w') as f:
        f.write("test")
    os.remove(test_file)
    logger.info("✅ Data directory is writable")
except Exception as e:
    logger.error(f"❌ Data directory write test failed: {e}")

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID")) if os.getenv("REPORT_CHANNEL_ID") else None
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
MONITORING_INTERVAL = int(os.getenv("MONITORING_INTERVAL", "20"))  # minutes
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "8000"))

# Validate required environment variables
if not TOKEN:
    logger.error("DISCORD_BOT_TOKEN not found in environment variables")
    sys.exit(1)

# API Endpoints
RPC_URL_PCHAIN = "https://api.juneo-mainnet.network/ext/bc/P"
RPC_URL_JUNE_CHAIN = "https://rpc.juneo-mainnet.network/ext/bc/JUNE/rpc"
RPC_URL_INFO = "https://rpc.juneo-mainnet.network/ext/info"

# Prometheus metrics
COMMAND_COUNTER = Counter('discord_bot_commands_total', 'Total commands executed', ['command', 'status'])
API_REQUEST_DURATION = Histogram('api_request_duration_seconds', 'API request duration', ['endpoint'])
NOTIFICATION_COUNTER = Counter('discord_bot_notifications_total', 'Total notifications sent', ['type', 'status'])
ACTIVE_SUBSCRIBERS = Gauge('discord_bot_active_subscribers', 'Number of active subscribers')
MONITORED_NODES = Gauge('discord_bot_monitored_nodes', 'Number of monitored nodes')
BOT_UPTIME = Gauge('discord_bot_uptime_seconds', 'Bot uptime in seconds')

class DatabaseManager:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self.init_database()
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections with proper error handling"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, timeout=30.0)
            conn.row_factory = sqlite3.Row  # Enable dict-like access
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def init_database(self):
        """Initialize database tables"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Subscribers table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, node_id)
                )
            ''')
            
            # Notifications table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    notification_type TEXT DEFAULT 'disconnected',
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    success BOOLEAN DEFAULT TRUE
                )
            ''')
            
            # Bot metrics table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
            logger.info("Database initialized successfully")
    
    def add_subscriber(self, user_id: str, node_id: str, node_name: str) -> bool:
        """Add a new subscriber"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO subscribers (user_id, node_id, node_name) VALUES (?, ?, ?)",
                    (user_id, node_id, node_name)
                )
                conn.commit()
                logger.info(f"Added subscriber: user_id={user_id}, node_id={node_id}")
                return True
        except Exception as e:
            logger.error(f"Error adding subscriber: {e}")
            return False
    
    def remove_subscriber(self, user_id: str, node_id: str) -> bool:
        """Remove a subscriber"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM subscribers WHERE user_id = ? AND node_id = ?",
                    (user_id, node_id)
                )
                conn.commit()
                logger.info(f"Removed subscriber: user_id={user_id}, node_id={node_id}")
                return True
        except Exception as e:
            logger.error(f"Error removing subscriber: {e}")
            return False
    
    def get_user_subscriptions(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all subscriptions for a user"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT node_id, node_name FROM subscribers WHERE user_id = ?",
                    (user_id,)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting user subscriptions: {e}")
            return []
    
    def get_all_subscriptions(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get all subscriptions grouped by user"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT user_id, node_id, node_name FROM subscribers")
                rows = cursor.fetchall()
                
                subscriptions = {}
                for row in rows:
                    user_id = row['user_id']
                    if user_id not in subscriptions:
                        subscriptions[user_id] = []
                    subscriptions[user_id].append({
                        'node_id': row['node_id'],
                        'node_name': row['node_name']
                    })
                return subscriptions
        except Exception as e:
            logger.error(f"Error getting all subscriptions: {e}")
            return {}
    
    def can_send_notification(self, user_id: str, node_id: str, hours: int = 24) -> bool:
        """Check if we can send a notification (rate limiting)"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cutoff_time = datetime.now() - timedelta(hours=hours)
                cursor.execute(
                    "SELECT COUNT(*) as count FROM notifications WHERE user_id = ? AND node_id = ? AND sent_at > ?",
                    (user_id, node_id, cutoff_time)
                )
                count = cursor.fetchone()['count']
                return count == 0
        except Exception as e:
            logger.error(f"Error checking notification eligibility: {e}")
            return False
    
    def record_notification(self, user_id: str, node_id: str, success: bool = True, 
                          notification_type: str = 'disconnected'):
        """Record a sent notification"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO notifications (user_id, node_id, notification_type, success) VALUES (?, ?, ?, ?)",
                    (user_id, node_id, notification_type, success)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error recording notification: {e}")
    
    def cleanup_old_records(self, days: int = 30):
        """Clean up old notification records"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cutoff_time = datetime.now() - timedelta(days=days)
                cursor.execute("DELETE FROM notifications WHERE sent_at < ?", (cutoff_time,))
                cursor.execute("DELETE FROM metrics WHERE recorded_at < ?", (cutoff_time,))
                deleted_notifications = cursor.rowcount
                conn.commit()
                logger.info(f"Cleaned up {deleted_notifications} old records")
        except Exception as e:
            logger.error(f"Error cleaning up old records: {e}")

# Initialize database manager
db_manager = DatabaseManager(DB_FILE)

class RateLimiter:
    def __init__(self, max_messages_per_minute: int = 25, max_burst: int = 3):
        self.max_messages_per_minute = max_messages_per_minute
        self.max_burst = max_burst
        self.message_queue = deque()
        self.last_message_times = deque()
        self.is_processing = False
        logger.info(f"Rate limiter initialized: {max_messages_per_minute}/min, {max_burst} burst")
    
    async def can_send_message(self) -> bool:
        """Check if we can send a message without hitting rate limits"""
        now = time.time()
        
        # Remove timestamps older than 1 minute
        while self.last_message_times and now - self.last_message_times[0] > 60:
            self.last_message_times.popleft()
        
        # Check limits
        if len(self.last_message_times) >= self.max_messages_per_minute:
            return False
        
        recent_messages = sum(1 for t in self.last_message_times if now - t < 5)
        if recent_messages >= self.max_burst:
            return False
        
        return True
    
    async def add_message_to_queue(self, user_id: str, node_id: str, node_name: str, validator_info: Dict[str, Any]):
        """Add a message to the queue for rate-limited sending"""
        message_data = {
            'user_id': user_id,
            'node_id': node_id,
            'node_name': node_name,
            'validator_info': validator_info,
            'timestamp': time.time()
        }
        self.message_queue.append(message_data)
        
        if not self.is_processing:
            asyncio.create_task(self.process_message_queue())
    
    async def process_message_queue(self):
        """Process the message queue with rate limiting"""
        self.is_processing = True
        
        try:
            while self.message_queue:
                if await self.can_send_message():
                    message_data = self.message_queue.popleft()
                    
                    # Check if message is still relevant (not too old)
                    if time.time() - message_data['timestamp'] < 300:  # 5 minutes
                        success = await self._send_notification(
                            message_data['user_id'],
                            message_data['node_id'],
                            message_data['node_name'],
                            message_data['validator_info']
                        )
                        
                        if success:
                            self.last_message_times.append(time.time())
                            await asyncio.sleep(2)  # Small delay between messages
                    else:
                        logger.warning(f"Discarding old notification for user {message_data['user_id']}, node {message_data['node_id']}")
                else:
                    await asyncio.sleep(10)  # Wait before checking again
        except Exception as e:
            logger.error(f"Error processing message queue: {e}")
        finally:
            self.is_processing = False
    
    async def _send_notification(self, user_id: str, node_id: str, node_name: str, validator_info: Dict[str, Any]) -> bool:
        """Internal method to send the actual notification"""
        try:
            user = bot.get_user(int(user_id))
            if user is None:
                user = await bot.fetch_user(int(user_id))
            
            embed = discord.Embed(
                title="⚠️ Node Alert",
                description=f"Your subscribed node **{node_name}** is not connected!",
                color=discord.Color.red()
            ).add_field(
                name="NodeID",
                value=node_id,
                inline=False
            ).add_field(
                name="Current Uptime",
                value=f"{validator_info['uptime']}%",
                inline=False
            ).add_field(
                name="Status",
                value="🔴 Disconnected",
                inline=False
            ).add_field(
                name="Next Notification",
                value="You will receive another notification in 24 hours if the issue persists.\nUse `/unsub` to stop these alerts.",
                inline=False
            )
            
            # Try to send DM
            try:
                await user.send(embed=embed)
                logger.info(f"Sent DM notification to user {user_id} for node {node_id}")
                db_manager.record_notification(user_id, node_id, True, 'dm')
                NOTIFICATION_COUNTER.labels(type='dm', status='success').inc()
                return True
            except discord.Forbidden:
                logger.warning(f"Cannot send DM to user {user_id}, trying channel ping")
                await ping_in_channel(user_id, node_id, node_name, validator_info)
                return True
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    logger.warning(f"Rate limited when sending DM to user {user_id}")
                    NOTIFICATION_COUNTER.labels(type='dm', status='rate_limited').inc()
                    return False
                else:
                    logger.error(f"HTTP error sending DM to user {user_id}: {e}")
                    NOTIFICATION_COUNTER.labels(type='dm', status='error').inc()
                    return False
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")
            NOTIFICATION_COUNTER.labels(type='dm', status='error').inc()
            return False

# Initialize components
rate_limiter = RateLimiter(max_messages_per_minute=25, max_burst=3)
bot_start_time = time.time()

# Initialize the bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='.', intents=intents)

class APIClient:
    """Centralized API client with retry logic and monitoring"""
    
    def __init__(self, timeout: int = 15, max_retries: int = 3):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.session = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def post_request(self, url: str, payload: Dict[str, Any], endpoint_name: str = "unknown") -> Optional[Dict[str, Any]]:
        """Make a POST request with retry logic and monitoring"""
        with API_REQUEST_DURATION.labels(endpoint=endpoint_name).time():
            for attempt in range(self.max_retries):
                try:
                    async with self.session.post(url, json=payload) as response:
                        response.raise_for_status()
                        return await response.json()
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout on attempt {attempt + 1} for {endpoint_name}")
                    if attempt == self.max_retries - 1:
                        raise
                except aiohttp.ClientError as e:
                    logger.warning(f"Client error on attempt {attempt + 1} for {endpoint_name}: {e}")
                    if attempt == self.max_retries - 1:
                        raise
                except Exception as e:
                    logger.error(f"Unexpected error on attempt {attempt + 1} for {endpoint_name}: {e}")
                    if attempt == self.max_retries - 1:
                        raise
                
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
        
        return None

async def fetch_validator_info(node_id: str) -> Optional[Dict[str, Any]]:
    """Fetch validator information for a single node"""
    async with APIClient() as client:
        payload = {
            "jsonrpc": "2.0",
            "method": "platform.getCurrentValidators",
            "params": {"nodeIDs": [node_id]},
            "id": 1
        }
        
        try:
            result = await client.post_request(RPC_URL_PCHAIN, payload, "validator_info")
            if result and "result" in result:
                validators = result["result"].get("validators", [])
                return validators[0] if validators else None
        except Exception as e:
            logger.error(f"Error fetching validator info for {node_id}: {e}")
        
        return None

async def fetch_multiple_validators(node_ids: List[str]) -> List[Dict[str, Any]]:
    """Fetch validator info for multiple nodes with batching and rate limiting"""
    if not node_ids:
        return []

    BATCH_SIZE = 15
    INTER_BATCH_DELAY_SECONDS = 5
    all_validators = []
    
    try:
        async with APIClient() as client:
            logger.info(f"Fetching validator info for {len(node_ids)} nodes in batches of {BATCH_SIZE}")
            
            for i in range(0, len(node_ids), BATCH_SIZE):
                batch_ids = node_ids[i:i + BATCH_SIZE]
                
                payload = {
                    "jsonrpc": "2.0",
                    "method": "platform.getCurrentValidators",
                    "params": {"nodeIDs": batch_ids},
                    "id": 1
                }
                
                try:
                    result = await client.post_request(RPC_URL_PCHAIN, payload, "batch_validators")
                    if result and "result" in result:
                        validators = result["result"].get("validators", [])
                        all_validators.extend(validators)
                        logger.info(f"Batch {i//BATCH_SIZE + 1} complete. Fetched {len(validators)} validators.")
                
                except Exception as e:
                    logger.error(f"Batch {i//BATCH_SIZE + 1} failed: {e}")
                
                # Rate limiting delay
                if i + BATCH_SIZE < len(node_ids):
                    await asyncio.sleep(INTER_BATCH_DELAY_SECONDS)
    
    except Exception as e:
        logger.error(f"Fatal error during validator fetch: {e}")
    
    logger.info(f"Fetch complete. Total validators retrieved: {len(all_validators)}")
    return all_validators

async def fetch_peer_info(node_id: str) -> Optional[Dict[str, Any]]:
    """Fetch peer information for a node"""
    async with APIClient() as client:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "info.peers",
            "params": {"nodeIDs": [node_id]}
        }
        
        try:
            result = await client.post_request(RPC_URL_INFO, payload, "peer_info")
            if result and "result" in result:
                peers = result["result"].get("peers", [])
                return peers[0] if peers else None
        except Exception as e:
            logger.error(f"Error fetching peer info for {node_id}: {e}")
        
        return None

async def fetch_block_height_pchain() -> str:
    """Fetch P-chain block height"""
    async with APIClient() as client:
        payload = {
            "jsonrpc": "2.0",
            "method": "platform.getHeight",
            "params": {},
            "id": 1
        }
        
        try:
            result = await client.post_request(RPC_URL_PCHAIN, payload, "pchain_height")
            if result and "result" in result:
                return str(result["result"].get("height", "N/A"))
        except Exception as e:
            logger.error(f"Error fetching P-chain height: {e}")
        
        return "N/A"

async def fetch_block_height_june_chain() -> str:
    """Fetch JUNE-chain block height"""
    async with APIClient() as client:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }
        
        try:
            result = await client.post_request(RPC_URL_JUNE_CHAIN, payload, "june_chain_height")
            if result and "result" in result:
                return str(int(result["result"], 16))  # Convert from hex to decimal
        except Exception as e:
            logger.error(f"Error fetching JUNE-chain height: {e}")
        
        return "N/A"

def get_uptime_bar(uptime_percentage: float) -> str:
    """Generate visual uptime bar"""
    total_blocks = 10
    filled_blocks = int(uptime_percentage // 10)
    partial_block_percentage = uptime_percentage % 10
    bar = []

    for i in range(total_blocks):
        if i < filled_blocks:
            bar.append("🟩")
        elif i == filled_blocks:
            bar.append("🟨" if partial_block_percentage >= 5 else "🟥")
        else:
            bar.append("🟥")

    return ''.join(bar)

def format_peer_info(peer_info: Optional[Dict[str, Any]], node_id: str) -> discord.Embed:
    """Format peer information into Discord embed"""
    status_color = discord.Color.green() if peer_info and peer_info.get("nodeID") else discord.Color.red()

    embed = discord.Embed(
        title="Peer Node Status",
        color=status_color
    )

    if peer_info:
        status = "Connected" if peer_info.get("nodeID") else "Disconnected"
        observed_uptime = peer_info.get("observedUptime", "0") + "%"
        embed.add_field(name="**Status:**", value=status, inline=False)
        embed.add_field(name="**Peer Uptime:**", value=observed_uptime, inline=False)
    else:
        embed.add_field(name="**Status:**", value="Disconnected", inline=False)
        embed.add_field(name="**Observed Uptime:**", value="0%", inline=False)

    embed.add_field(name="**NodeID:**", value=node_id, inline=False)
    embed.add_field(
        name="**Note:**",
        value="1. For validators, use `/sub` for detailed monitoring\n2. Use `/help` for assistance",
        inline=False
    )
    embed.set_footer(text="Juneo Network")

    return embed


def calculate_potential_rewards_with_fees(validator_info: Dict[str, Any]) -> float:
    """
    Calculate the potential rewards with delegation fees for a validator.
    
    Args:
        validator_info: Validator information from the RPC response
        
    Returns:
        float: Total potential rewards including delegation fees in JUNE
    """
    try:
        # Get base potential reward for the validator
        validator_potential_reward = float(validator_info.get('potentialReward', 0))
        
        # Get delegation fee percentage
        delegation_fee_str = validator_info.get('delegationFee', '0.0000')
        delegation_fee_percentage = float(delegation_fee_str) / 100.0  # Convert to decimal
        
        # Calculate total delegation rewards from all delegators
        total_delegation_rewards = 0.0
        delegators = validator_info.get('delegators', [])
        
        for delegator in delegators:
            delegator_potential_reward = float(delegator.get('potentialReward', 0))
            total_delegation_rewards += delegator_potential_reward
        
        # Calculate delegation fees (validator's cut from delegation rewards)
        delegation_fees = total_delegation_rewards * delegation_fee_percentage
        
        # Total potential rewards = validator reward + delegation fees
        total_potential_rewards = validator_potential_reward + delegation_fees
        
        # Convert from nJUNE to JUNE (divide by 1e9)
        return total_potential_rewards / 1e9
        
    except (ValueError, TypeError) as e:
        logger.error(f"Error calculating potential rewards with fees: {e}")
        return 0.0

async def format_validator_info(validator_info: Optional[Dict[str, Any]]) -> discord.Embed:
    """Format validator information into Discord embed"""
    if not validator_info:
        return discord.Embed(
            title="Error",
            description="No information found for the specified NodeID",
            color=discord.Color.red()
        )

    start_time = datetime.fromtimestamp(int(validator_info['startTime']))
    end_time = datetime.fromtimestamp(int(validator_info['endTime']))

    formatted_start_time = discord.utils.format_dt(start_time, style='F')
    formatted_end_time = discord.utils.format_dt(end_time, style='F')

    status_color = discord.Color.green() if validator_info['connected'] else discord.Color.red()
    uptime_percentage = float(validator_info['uptime'])
    rounded_uptime = round(uptime_percentage)

    status_indicator = ":green_circle:" if validator_info['connected'] else ":red_circle:"
    uptime_bar = get_uptime_bar(uptime_percentage)
    
    # Calculate potential rewards with fees
    potential_rewards_with_fees = calculate_potential_rewards_with_fees(validator_info)
    base_potential_reward = int(validator_info['potentialReward']) / 1e9

    embed = discord.Embed(
        title="Validator Node Status",
        color=status_color
    ).add_field(
        name="**Status:**",
        value=f"{status_indicator} {'Connected' if validator_info['connected'] else 'Disconnected'}",
        inline=False
    ).add_field(
        name="**Uptime:**",
        value=f"{uptime_bar} ({uptime_percentage:.2f}%)",
        inline=False
    ).add_field(
        name="**Stake Amount:**",
        value=f"{int(validator_info['stakeAmount']) / 1e9:.2f} JUNE",
        inline=False
    ).add_field(
        name="**Start Time:**",
        value=formatted_start_time,
        inline=False
    ).add_field(
        name="**End Time:**",
        value=formatted_end_time,
        inline=False
    ).add_field(
        name="**Delegation Fee:**",
        value=f"{validator_info['delegationFee']}%",
        inline=False
    ).add_field(
        name="**Potential Reward:**",
        value=f"{base_potential_reward:.2f} JUNE",
        inline=False
    ).add_field(
        name="**Potential Rewards with Fees:**",
        value=f"{potential_rewards_with_fees:.2f} JUNE",
        inline=False
    ).add_field(
        name="**Delegator Count:**",
        value=str(validator_info['delegatorCount']),
        inline=False
    ).add_field(
        name="**Delegator Weight:**",
        value=f"{int(validator_info['delegatorWeight']) / 1e9:.2f} JUNE",
        inline=False
    )

    return embed

async def ping_in_channel(user_id: str, node_id: str, node_name: str, validator_info: Dict[str, Any]):
    """Ping user in the report channel if DM fails"""
    if not REPORT_CHANNEL_ID:
        logger.warning("No report channel configured")
        return
    
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel is None:
            logger.error(f"Report channel {REPORT_CHANNEL_ID} not found")
            return
        
        embed = discord.Embed(
            title="⚠️ Node Alert",
            description=f"Node is not connected!",
            color=discord.Color.red()
        ).add_field(
            name="NodeID",
            value=node_id,
            inline=False
        ).add_field(
            name="Current Uptime",
            value=f"{validator_info['uptime']}%",
            inline=False
        ).add_field(
            name="Status",
            value="🔴 Disconnected",
            inline=False
        ).add_field(
            name="Note",
            value="This notification is sent because DM delivery failed.\nUse `/unsub` to stop these alerts.",
            inline=False
        )
        
        await channel.send(f"<@{user_id}> Your node needs attention!", embed=embed)
        logger.info(f"Sent channel ping to user {user_id} for node {node_id}")
        db_manager.record_notification(user_id, node_id, True, 'channel_ping')
        NOTIFICATION_COUNTER.labels(type='channel_ping', status='success').inc()
    except Exception as e:
        logger.error(f"Error pinging in channel for user {user_id}: {e}")
        NOTIFICATION_COUNTER.labels(type='channel_ping', status='error').inc()

@tasks.loop(minutes=MONITORING_INTERVAL)
async def monitor_subscribed_nodes():
    """Monitor all subscribed nodes and notify users of issues"""
    try:
        logger.info("Starting monitoring check...")
        subscriptions = db_manager.get_all_subscriptions()
        
        if not subscriptions:
            logger.info("No subscribers found")
            return
        
        # Update metrics
        ACTIVE_SUBSCRIBERS.set(len(subscriptions))
        
        # Clean up old records
        db_manager.cleanup_old_records()
        
        # Collect all unique node IDs
        all_node_ids = set()
        user_nodes = {}
        
        for user_id, user_subs in subscriptions.items():
            user_nodes[user_id] = []
            for sub in user_subs:
                node_id = sub['node_id']
                node_name = sub['node_name']
                all_node_ids.add(node_id)
                user_nodes[user_id].append((node_id, node_name))
        
        if not all_node_ids:
            logger.info("No node IDs to monitor")
            return
        
        MONITORED_NODES.set(len(all_node_ids))
        logger.info(f"Monitoring {len(all_node_ids)} unique nodes for {len(user_nodes)} users")
        
        # Fetch validator info for all nodes
        validators = await fetch_multiple_validators(list(all_node_ids))
        current_time = int(time.time())
        
        # Create lookup for validator info
        validator_lookup = {v['nodeID']: v for v in validators}
        
        notifications_queued = 0
        
        # Check each user's nodes
        for user_id, nodes in user_nodes.items():
            for node_id, node_name in nodes:
                validator_info = validator_lookup.get(node_id)
                
                if not validator_info:
                    logger.warning(f"No validator info found for node {node_id}")
                    continue
                
                # Check if node is in validation period
                start_time = int(validator_info['startTime'])
                end_time = int(validator_info['endTime'])
                in_validation_period = start_time <= current_time <= end_time
                
                # Only notify if node is not connected AND is in validation period AND we haven't notified recently
                if (not validator_info['connected'] and 
                    in_validation_period and 
                    db_manager.can_send_notification(user_id, node_id)):
                    
                    # Queue notification for rate-limited sending
                    await rate_limiter.add_message_to_queue(user_id, node_id, node_name, validator_info)
                    notifications_queued += 1
        
        logger.info(f"Completed monitoring check. Queued {notifications_queued} notifications")
    
    except Exception as e:
        logger.error(f"Error during monitoring: {e}")

@tasks.loop(minutes=1)
async def update_bot_metrics():
    """Update bot uptime and other metrics"""
    try:
        current_uptime = time.time() - bot_start_time
        BOT_UPTIME.set(current_uptime)
    except Exception as e:
        logger.error(f"Error updating bot metrics: {e}")

def handle_command_metric(command_name: str, success: bool):
    """Helper function to record command metrics"""
    status = 'success' if success else 'error'
    COMMAND_COUNTER.labels(command=command_name, status=status).inc()

async def graceful_shutdown(signum, frame):
    """Handle graceful shutdown"""
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    
    # Stop monitoring tasks
    if monitor_subscribed_nodes.is_running():
        monitor_subscribed_nodes.stop()
    if update_bot_metrics.is_running():
        update_bot_metrics.stop()
    
    # Close bot
    await bot.close()
    logger.info("Bot shutdown complete")
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(graceful_shutdown(s, f)))
signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(graceful_shutdown(s, f)))

@bot.event
async def on_ready():
    """Bot ready event handler"""
    logger.info(f'Bot logged in as {bot.user}')
    
    # Sync commands
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        try:
            synced_commands = await bot.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced_commands)} commands to guild {GUILD_ID}")
        except Exception as e:
            logger.error(f"Error syncing commands to guild: {e}")
    else:
        try:
            synced_commands = await bot.tree.sync()
            logger.info(f"Synced {len(synced_commands)} commands globally")
        except Exception as e:
            logger.error(f"Error syncing commands globally: {e}")
    
    # Start monitoring tasks
    if not monitor_subscribed_nodes.is_running():
        monitor_subscribed_nodes.start()
        logger.info(f"Started node monitoring task (checks every {MONITORING_INTERVAL} minutes)")
    
    if not update_bot_metrics.is_running():
        update_bot_metrics.start()
        logger.info("Started metrics update task")
    
    # Start Prometheus metrics server
    try:
        start_http_server(PROMETHEUS_PORT)
        logger.info(f"Prometheus metrics server started on port {PROMETHEUS_PORT}")
    except Exception as e:
        logger.error(f"Failed to start Prometheus server: {e}")

@bot.event
async def on_command_error(ctx, error):
    """Global error handler"""
    logger.error(f"Command error in {ctx.command}: {error}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global app command error handler"""
    logger.error(f"App command error in {interaction.command}: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred while processing your command.", ephemeral=True)

@bot.tree.command(name='check', description='Check the status of a peer node')
@app_commands.describe(node_id='The NodeID of the peer')
async def check(interaction: discord.Interaction, node_id: str):
    """Check peer node status"""
    await interaction.response.defer()
    command_success = False
    
    try:
        peer_info = await fetch_peer_info(node_id)
        embed = format_peer_info(peer_info, node_id)
        await interaction.followup.send(embed=embed)
        command_success = True
        logger.info(f"User {interaction.user.id} checked peer {node_id}")
    except Exception as e:
        logger.error(f"Error in check command: {e}")
        await interaction.followup.send(f"Error checking node status: {e}", ephemeral=True)
    finally:
        handle_command_metric('check', command_success)

@bot.tree.command(name='sub', description='Subscribe to a NodeID for monitoring')
@app_commands.describe(node_id='The NodeID to subscribe to', name='Custom name for the NodeID')
async def sub(interaction: discord.Interaction, node_id: str, name: str = None):
    """Subscribe to node monitoring"""
    await interaction.response.defer(ephemeral=True)
    command_success = False
    
    try:
        user_id = str(interaction.user.id)
        
        # Check for existing subscription
        user_subs = db_manager.get_user_subscriptions(user_id)
        if any(sub['node_id'] == node_id for sub in user_subs):
            await interaction.followup.send(f"You are already subscribed to NodeID `{node_id}`", ephemeral=True)
            return

        # Add subscription without validation - we'll check during monitoring
        node_name = name or node_id[:16] + "..." if len(node_id) > 16 else node_id
        success = db_manager.add_subscriber(user_id, node_id, node_name)
        
        if success:
            embed = discord.Embed(
                title="✅ Subscription Successful",
                description=f"Subscribed to NodeID `{node_id}` with name `{node_name}`",
                color=discord.Color.green()
            ).add_field(
                name="Monitoring Details",
                value=f"• Bot checks every {MONITORING_INTERVAL} minutes\n• Notifications sent once per day maximum\n• Only notified during validation periods\n• DM preferred, channel ping as backup",
                inline=False
            ).add_field(
                name="Note",
                value="The bot will automatically detect if this NodeID is an active validator during monitoring.",
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            command_success = True
            logger.info(f"User {user_id} subscribed to node {node_id} with name {node_name}")
        else:
            await interaction.followup.send("Failed to add subscription. Please try again.", ephemeral=True)
    
    except Exception as e:
        logger.error(f"Error in sub command: {e}")
        await interaction.followup.send(f"Error subscribing to node: {e}", ephemeral=True)
    finally:
        handle_command_metric('sub', command_success)

@bot.tree.command(name='unsub', description='Unsubscribe from a NodeID')
async def unsub(interaction: discord.Interaction):
    """Unsubscribe from node monitoring"""
    await interaction.response.defer(ephemeral=True)
    command_success = False
    
    try:
        user_id = str(interaction.user.id)
        user_subs = db_manager.get_user_subscriptions(user_id)
        
        if not user_subs:
            await interaction.followup.send("You are not subscribed to any NodeID", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=sub['node_name'][:100], 
                value=sub['node_id'], 
                description=sub['node_id'][:100]
            )
            for sub in user_subs
        ]

        class UnsubSelect(discord.ui.Select):
            def __init__(self):
                super().__init__(placeholder='Choose a NodeID to unsubscribe from', options=options)

            async def callback(self, select_interaction: discord.Interaction):
                try:
                    selected_node_id = self.values[0]
                    success = db_manager.remove_subscriber(user_id, selected_node_id)
                    
                    if success:
                        await select_interaction.response.send_message(
                            f"✅ Unsubscribed from NodeID `{selected_node_id}`", 
                            ephemeral=True
                        )
                        logger.info(f"User {user_id} unsubscribed from node {selected_node_id}")
                    else:
                        await select_interaction.response.send_message(
                            "Failed to unsubscribe. Please try again.", 
                            ephemeral=True
                        )
                except Exception as e:
                    logger.error(f"Error in unsub callback: {e}")
                    await select_interaction.response.send_message(
                        "An error occurred while unsubscribing.", 
                        ephemeral=True
                    )

        view = discord.ui.View()
        view.add_item(UnsubSelect())
        await interaction.followup.send("Select a NodeID to unsubscribe from:", view=view, ephemeral=True)
        command_success = True
    
    except Exception as e:
        logger.error(f"Error in unsub command: {e}")
        await interaction.followup.send(f"Error processing unsubscription: {e}", ephemeral=True)
    finally:
        handle_command_metric('unsub', command_success)

@bot.tree.command(name='status', description='Get status of a subscribed NodeID')
async def status(interaction: discord.Interaction):
    """Get status of subscribed nodes"""
    await interaction.response.defer(ephemeral=True)
    command_success = False
    
    try:
        user_id = str(interaction.user.id)
        user_subs = db_manager.get_user_subscriptions(user_id)
        
        if not user_subs:
            await interaction.followup.send("You are not subscribed to any NodeID", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=sub['node_name'][:100], 
                value=sub['node_id'], 
                description=sub['node_id'][:100]
            )
            for sub in user_subs
        ]

        class StatusSelect(discord.ui.Select):
            def __init__(self):
                super().__init__(placeholder='Choose a NodeID to check status', options=options)

            async def callback(self, select_interaction: discord.Interaction):
                try:
                    await select_interaction.response.defer(ephemeral=True)
                    selected_node_id = self.values[0]
                    validator_info = await fetch_validator_info(selected_node_id)
                    embed = await format_validator_info(validator_info)
                    await select_interaction.followup.send(embed=embed, ephemeral=True)
                except Exception as e:
                    logger.error(f"Error in status callback: {e}")
                    await select_interaction.followup.send(
                        "An error occurred while fetching status.", 
                        ephemeral=True
                    )

        view = discord.ui.View()
        view.add_item(StatusSelect())
        await interaction.followup.send("Select a NodeID to get status:", view=view, ephemeral=True)
        command_success = True
    
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await interaction.followup.send(f"Error checking status: {e}", ephemeral=True)
    finally:
        handle_command_metric('status', command_success)

@bot.tree.command(name='list', description='List all subscribed NodeIDs and Names')
async def list_command(interaction: discord.Interaction):
    """List user subscriptions"""
    await interaction.response.defer(ephemeral=True)
    command_success = False
    
    try:
        user_id = str(interaction.user.id)
        user_subs = db_manager.get_user_subscriptions(user_id)

        if not user_subs:
            await interaction.followup.send("You are not subscribed to any NodeID", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Your Subscribed NodeIDs",
            description=f"You have {len(user_subs)} active subscription(s)",
            color=discord.Color.blue()
        )

        for i, sub in enumerate(user_subs, 1):
            embed.add_field(
                name=f"{i}. {sub['node_name']}", 
                value=f"`{sub['node_id']}`", 
                inline=False
            )

        embed.set_footer(text=f"Monitoring frequency: Every {MONITORING_INTERVAL} minutes | Max notifications: 1 per day per node")

        # Send the embed as a direct message
        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send("📬 I have sent you a DM with your subscribed NodeIDs and Names.", ephemeral=True)
            command_success = True
            logger.info(f"User {user_id} listed their subscriptions")
        except discord.Forbidden:
            await interaction.followup.send("❌ Failed to send DM. Make sure your DMs are open.", ephemeral=True)
    
    except Exception as e:
        logger.error(f"Error in list command: {e}")
        await interaction.followup.send(f"Error retrieving subscriptions: {e}", ephemeral=True)
    finally:
        handle_command_metric('list', command_success)

@bot.tree.command(name='help', description='List all available commands')
async def help_command(interaction: discord.Interaction):
    """Show help information"""
    command_success = False
    
    try:
        embed = discord.Embed(
            title="🤖 Available Commands",
            description="Here are the commands you can use:",
            color=discord.Color.blue()
        ).add_field(
            name="/check <NodeID>", 
            value="Check the status of a validator node (peer info).",
            inline=False
        ).add_field(
            name="/sub <NodeID> [name]", 
            value="Subscribe to a NodeID for monitoring. Get notifications when it goes down during validation periods.",
            inline=False
        ).add_field(
            name="/unsub", 
            value="Unsubscribe from a NodeID.",
            inline=False
        ).add_field(
            name="/status", 
            value="Get detailed status of your subscribed NodeIDs.",
            inline=False
        ).add_field(
            name="/list",
            value="Get a DM with all your subscribed NodeIDs and names.",
            inline=False
        ).add_field(
            name="/help", 
            value="Display this help message.",
            inline=False
        ).add_field(
            name="🔔 Monitoring Info",
            value=f"• Checks every {MONITORING_INTERVAL} minutes\n• Max 1 notification per day per node\n• Only during validation periods\n• DM first, channel ping if DM fails",
            inline=False
        ).add_field(
            name="📊 Bot Health",
            value=f"• Metrics available on port {PROMETHEUS_PORT}\n• Structured logging enabled\n• Automatic error recovery",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        command_success = True
        logger.info(f"User {interaction.user.id} requested help")
    
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        await interaction.response.send_message("Error displaying help information.", ephemeral=True)
    finally:
        handle_command_metric('help', command_success)

@bot.tree.command(name='stats', description='Show bot statistics (Admin only)')
async def stats_command(interaction: discord.Interaction):
    """Show bot statistics"""
    await interaction.response.defer(ephemeral=True)
    command_success = False
    
    try:
        # Basic permission check (you can enhance this)
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ This command requires administrator permissions.", ephemeral=True)
            return
        
        subscriptions = db_manager.get_all_subscriptions()
        total_subscribers = len(subscriptions)
        total_nodes = len(set(sub['node_id'] for subs in subscriptions.values() for sub in subs))
        
        uptime_seconds = time.time() - bot_start_time
        uptime_str = str(timedelta(seconds=int(uptime_seconds)))
        
        embed = discord.Embed(
            title="📊 Bot Statistics",
            color=discord.Color.blue()
        ).add_field(
            name="👥 Active Subscribers",
            value=str(total_subscribers),
            inline=True
        ).add_field(
            name="🔗 Monitored Nodes",
            value=str(total_nodes),
            inline=True
        ).add_field(
            name="⏱️ Uptime",
            value=uptime_str,
            inline=True
        ).add_field(
            name="🔄 Check Interval",
            value=f"{MONITORING_INTERVAL} minutes",
            inline=True
        ).add_field(
            name="📈 Metrics Port",
            value=str(PROMETHEUS_PORT),
            inline=True
        ).add_field(
            name="💾 Database",
            value="SQLite",
            inline=True
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        command_success = True
        logger.info(f"Admin {interaction.user.id} checked bot stats")
    
    except Exception as e:
        logger.error(f"Error in stats command: {e}")
        await interaction.followup.send(f"Error retrieving statistics: {e}", ephemeral=True)
    finally:
        handle_command_metric('stats', command_success)

def main():
    """Main function to run the bot"""
    if not TOKEN:
        logger.error("DISCORD_BOT_TOKEN not found in environment variables")
        sys.exit(1)
    
    logger.info("Starting Discord bot...")
    logger.info(f"Monitoring interval: {MONITORING_INTERVAL} minutes")
    logger.info(f"Prometheus port: {PROMETHEUS_PORT}")
    logger.info(f"Database file: {DB_FILE}")
    
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()