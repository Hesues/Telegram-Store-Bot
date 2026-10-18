module.exports = {
  apps: [
    {
      name: "cubes-bot",
      script: "main.py",
      interpreter: "/root/cubesbot-app/CUBESBOT/venv/bin/python",
      cwd: "/root/cubesbot-app/CUBESBOT",
      autorestart: true,
      watch: false,
      max_memory_restart: '300M',
      env: {
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
