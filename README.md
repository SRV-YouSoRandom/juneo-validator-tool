# Juneo Discord Bot with Monitoring

A production-ready Discord bot for monitoring Juneo validator nodes with comprehensive observability stack.

## 🚀 Quick Start

```bash
# 1. Clone/setup project
mkdir discord-bot && cd discord-bot
# ... copy all files from artifacts

# 2. Make setup script executable and run
chmod +x setup.sh
./setup.sh

# 3. Configure your bot
cp .env.docker .env
nano .env  # Add your Discord bot token

# 4. Start the stack
docker-compose up -d

# 5. Check status
docker-compose ps
```

## 📊 Access Points

- **Grafana Dashboard**: http://localhost:3000
- **Prometheus Metrics**: http://localhost:9090
- **Bot Metrics**: http://localhost:8000/metrics
- **Alertmanager**: http://localhost:9093

## 🤖 Bot Commands

| Command | Description |
|---------|-------------|
| `/check <NodeID>` | Check peer node status |
| `/sub <NodeID> [name]` | Subscribe to node monitoring |
| `/unsub` | Unsubscribe from node monitoring |
| `/status` | Get detailed status of subscribed nodes |
| `/height` | Get P-chain and JUNE-chain block heights |
| `/list` | List all subscribed NodeIDs |
| `/help` | Show help information |
| `/stats` | Show bot statistics (Admin only) |

## 🔧 Features

### Discord Bot
- ✅ SQLite database for reliable data persistence
- ✅ Rate limiting and retry logic for API calls
- ✅ Comprehensive error handling and logging
- ✅ Health checks and graceful shutdown
- ✅ Prometheus metrics integration
- ✅ Smart notification system (max 1/day per node)

### Monitoring Stack
- ✅ Real-time performance metrics
- ✅ Command usage analytics
- ✅ API response time tracking
- ✅ Notification delivery monitoring
- ✅ Custom Grafana dashboards
- ✅ Alerting capabilities

### Infrastructure
- ✅ Docker containerization with multi-stage builds
- ✅ Persistent data volumes
- ✅ Automatic service restarts
- ✅ Security hardening (non-root containers)
- ✅ Network isolation

## 📈 Monitoring Metrics

The bot exposes comprehensive metrics for monitoring:

- **Command Metrics**: Usage statistics for all bot commands
- **API Performance**: Response times for Juneo network calls
- **Notification Stats**: Success/failure rates for user notifications
- **System Health**: Bot uptime, subscriber counts, monitored nodes
- **Error Tracking**: Failed operations and their frequencies

## 🔒 Security

- Environment variables for sensitive configuration
- Non-root container execution
- Internal Docker network isolation
- Authenticated Grafana access
- Proper file permissions and ownership

## 🛠️ Management Commands

```bash
# View logs
docker-compose logs -f discord-bot

# Restart services
docker-compose restart discord-bot

# Update services
docker-compose pull && docker-compose up -d

# Backup database
docker cp $(docker-compose ps -q discord-bot):/app/data/bot_data.db ./backup.db

# Access container
docker-compose exec discord-bot /bin/bash

# Clean shutdown
docker-compose down
```

## 📊 Dashboard Features

The Grafana dashboard includes:
- Command usage pie charts
- Real-time subscriber and node counts
- API response time percentiles
- Notification success rates
- Bot uptime tracking
- Error rate monitoring

## ⚙️ Configuration

### Environment Variables (.env)
```bash
DISCORD_BOT_TOKEN=your_bot_token_here
REPORT_CHANNEL_ID=channel_id_for_fallback_notifications
GUILD_ID=your_guild_id
MONITORING_INTERVAL=20  # minutes
GRAFANA_USER=admin
GRAFANA_PASSWORD=admin123
```

### Monitoring Intervals
- Node checks: Every 20 minutes (configurable)
- Metrics updates: Every minute
- Prometheus scraping: Every 30 seconds

## 🚨 Alerting

Configure alerts in `monitoring/alertmanager.yml` for:
- Bot downtime
- High error rates
- API latency issues
- Failed notifications

## 🔧 Troubleshooting

### Common Issues

**Bot won't start**
```bash
# Check logs
docker-compose logs discord-bot
# Verify token in .env file
```

**No metrics in Grafana**
```bash
# Check Prometheus targets
curl http://localhost:9090/targets
# Verify bot metrics endpoint
curl http://localhost:8000/metrics
```

**Database errors**
```bash
# Check permissions
docker-compose exec discord-bot ls -la /app/data/
# Verify SQLite database
docker-compose exec discord-bot sqlite3 /app/data/bot_data.db ".tables"
```

### Log Analysis
```bash
# Real-time logs
docker-compose logs -f discord-bot

# Search for errors
docker-compose logs discord-bot | grep ERROR

# Check specific timeframe
docker-compose logs --since="1h" discord-bot
```

## 📋 Maintenance

### Regular Tasks
- Monitor disk usage for data volumes
- Check bot performance metrics
- Review error logs
- Update dependencies periodically
- Backup database regularly

### Updates
```bash
# Pull latest images
docker-compose pull

# Rebuild bot image
docker-compose build discord-bot

# Apply updates
docker-compose up -d
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📄 License

[Your chosen license here]

## 🆘 Support

For issues and questions:
1. Check the troubleshooting section
2. Review logs for error details
3. Open an issue with full context
4. Include relevant log snippets