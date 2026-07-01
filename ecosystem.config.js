// PM2 Ecosystem Config — Hedge Enrichment (3 parallel chunks)
//
// 3,766 managers split across 3 processes:
//   chunk-1 : managers   0–1254  (~1255 managers)
//   chunk-2 : managers 1255–2509 (~1255 managers)
//   chunk-3 : managers 2510–3766 (~1256 managers)
//
// Each chunk hits the API at 5 RPM → 15 RPM total (free-tier safe).
// On a paid key, raise rpm to 14 each → 42 RPM total → ~3 hrs.
//
// Deploy:
//   scp -r . user@YOUR_DO_IP:/app/hedge_enrichment
//   ssh user@YOUR_DO_IP
//   cd /app/hedge_enrichment && pip install -r requirements.txt
//   GOOGLE_API_KEY=YOUR_KEY pm2 start ecosystem.config.js
//
// Resume after crash:
//   GOOGLE_API_KEY=YOUR_KEY pm2 start ecosystem.config.js --env resume
//
// Monitor:
//   pm2 logs          # live logs all 3
//   pm2 logs chunk-1  # single chunk
//   pm2 status        # process table

module.exports = {
  apps: [
    {
      name: "chunk-1",
      script: "agent.py",
      interpreter: "/root/hedge_enrichment/venv/bin/python3",
      args: [
        "--key",           process.env.GOOGLE_API_KEY || "",
        "--model",         "google:gemini-2.5-flash",
        "--rpm",           "5",
        "--input",         "all_managers.csv",
        "--offset",        "0",
        "--limit",         "1255",
        "--output",        "output_chunk1.csv",
        "--progress-file", "progress_chunk1.json",
      ],
      env_resume: {
        RESUME: "1",
      },
      autorestart: false,   // agent has its own resume logic; don't loop on completion
      log_file:    "logs/chunk1.log",
      error_file:  "logs/chunk1_err.log",
      time:        true,
    },
    {
      name: "chunk-2",
      script: "agent.py",
      interpreter: "/root/hedge_enrichment/venv/bin/python3",
      args: [
        "--key",           process.env.GOOGLE_API_KEY || "",
        "--model",         "google:gemini-2.5-flash",
        "--rpm",           "5",
        "--input",         "all_managers.csv",
        "--offset",        "1255",
        "--limit",         "1255",
        "--output",        "output_chunk2.csv",
        "--progress-file", "progress_chunk2.json",
      ],
      env_resume: {
        RESUME: "1",
      },
      autorestart: false,
      log_file:    "logs/chunk2.log",
      error_file:  "logs/chunk2_err.log",
      time:        true,
    },
    {
      name: "chunk-3",
      script: "agent.py",
      interpreter: "/root/hedge_enrichment/venv/bin/python3",
      args: [
        "--key",           process.env.GOOGLE_API_KEY || "",
        "--model",         "google:gemini-2.5-flash",
        "--rpm",           "5",
        "--input",         "all_managers.csv",
        "--offset",        "2510",
        "--limit",         "1256",
        "--output",        "output_chunk3.csv",
        "--progress-file", "progress_chunk3.json",
      ],
      env_resume: {
        RESUME: "1",
      },
      autorestart: false,
      log_file:    "logs/chunk3.log",
      error_file:  "logs/chunk3_err.log",
      time:        true,
    },
  ],
};
