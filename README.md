# TgSHOP - Ready-made Telegram Store Bot

Introducing TgSHOP, a premade Telegram shop bot with a built-in web admin panel. Customers browse products, place orders, pay and track their purchases inside Telegram, while you manage stock, orders, pricing and themes from your phone or desktop. Launch your own Telegram storefront in minutes instead of weeks.

Contact: https://t.me/hesuse
---
Demo Video: https://t.me/readymadesource
---
## Additional Information

For clients interested in purchasing the TgSHOP template, we offer a fully functional Telegram store package. This includes:

- Telegram bot built with python-telegram-bot, plus a Flask web backend
- Product catalog with images, pricing and minimum order quantities
- Guided order flow: choose product, adjust quantity, pick courier and delivery type, then pay
- Courier and delivery options (Lalamove, J&T, JRS; Same Day, Normal, Custom), fully editable in code
- QR code payment flow (GCash QR included, swappable for any QR from the admin panel)
- Order management: Pending, Confirmed, Delivered and Cancelled statuses with one-tap admin buttons
- Instant order notifications to your private admin group
- Inventory system with stock tracking, restock commands, low-stock alerts and adjustable warning thresholds
- Customer order history, order lookup and VIP tier based on total spend
- Referral tracking for confirmed orders
- Telegram Mini App admin dashboard, opened straight from the bot
- Sales analytics: revenue, best sellers, order status breakdown, new vs returning users
- Add, edit and delete products from the panel (stored in SQLite, survives restarts)
- Seasonal theme system (Default, Christmas, Halloween, New Year, Summer) switchable live
- Broadcast announcements to all users
- Export orders to CSV or JSON
- Bulk order status updates
- Signed, expiring admin links and admin-only commands
- PM2 config included for easy VPS deployment
- And much more!

---

## Admin Commands

| Command | Description |
|---|---|
| `/admindashboard` | Open the Mini App admin dashboard |
| `/admincommands` | Show all admin commands |
| `/orders [status] [page]` | List orders with pagination |
| `/order <id>` | View a single order |
| `/user_orders` | View a customer's orders |
| `/orders_date <YYYY-MM-DD>` | Orders for a specific date |
| `/stats` | Sales statistics |
| `/top_products` | Best sellers |
| `/stocklist` / `/low_stock` / `/stock_alerts` | Inventory overview and alerts |
| `/restock <product> <amount>` | Add stock |
| `/set_warning_threshold` | Low-stock alert level |
| `/export_orders [status\|all] [csv\|json]` | Export orders |
| `/bulk_update_status <status> <ids...>` | Update many orders at once |
| `/set_theme <theme>` | Change the bot theme |

---

Screenshots
<img width="444" height="718" alt="image" src="https://github.com/user-attachments/assets/ebcf830d-b138-4b8e-927d-da7ffa785f54" />
<img width="438" height="699" alt="image" src="https://github.com/user-attachments/assets/b2f46270-e3da-4d26-afc9-8e7c61664edf" />
<img width="445" height="753" alt="image" src="https://github.com/user-attachments/assets/843e303a-075e-4a27-a35c-bd7f9b0c9ec4" />
<img width="439" height="702" alt="image" src="https://github.com/user-attachments/assets/bf4761b9-812d-4325-b6fe-f4aac456ce3d" />
<img width="441" height="712" alt="image" src="https://github.com/user-attachments/assets/88923b69-5788-4b8b-b052-8a91d215a8ed" />
<img width="439" height="699" alt="image" src="https://github.com/user-attachments/assets/5dc2348b-24dc-4c0f-b1c5-5250f6167500" />


## Supported Themes

- Default
- Christmas
- Halloween
- New Year
- Summer

---

## Tech Stack

- Python 3, python-telegram-bot, Flask
- SQLite database
- Telegram Mini App (WebApp) admin panel
- PM2 process manager

---

## Quick Start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy `.env.example` to `.env` and fill in your values:
   ```
   BOT_TOKEN=
   ADMIN_GROUP_ID=
   ADMIN_USER_IDS=
   WEBAPP_URL=
   AUTH_SECRET_KEY=
   DB_PATH=
   ORDER_PAGE_SIZE=
   VIP_MIN_INV=
   DEFAULT_WARNING_THRESHOLD=
   ```
3. Install dependencies and run:
   ```
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   python main.py
   ```
4. Or run it in the background with PM2: `pm2 start ecosystem.config.js`

---

## Contact Us

Contact us for access and purchase:

- Discord: hesues
- Telegram: https://t.me/hesuse

Note: The bot may receive updates over time, so the images shown above might not exactly reflect the most current design or features.





