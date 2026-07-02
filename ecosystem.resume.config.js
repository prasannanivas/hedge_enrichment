// PM2 Resume Config — adds --resume flag to all chunks
// Use this after a crash, server reboot, or manual stop.
//
// Resume all 3:
//   GOOGLE_API_KEY=YOUR_KEY pm2 start ecosystem.resume.config.js
//
// Resume one chunk only:
//   GOOGLE_API_KEY=YOUR_KEY pm2 start ecosystem.resume.config.js --only chunk-1

const PYTHON   = "/root/hedge_enrichment/venv/bin/python3";
const MODEL    = "google:gemini-2.5-flash";
const RPM      = "5";
const INPUT    = "all_managers.csv";
const API_KEY  = process.env.GOOGLE_API_KEY || "";

module.exports = {
  apps: [
    {
      name:        "chunk-1",
      script:      "agent.py",
      interpreter: PYTHON,
      args: [
        "--key",           API_KEY,
        "--model",         MODEL,
        "--rpm",           RPM,
        "--input",         INPUT,
        "--offset",        "0",
        "--limit",         "1255",
        "--output",        "output_chunk1.csv",
        "--progress-file", "progress_chunk1.json",
        "--resume",
      ],
      autorestart: false,
      log_file:    "logs/chunk1.log",
      error_file:  "logs/chunk1_err.log",
      time:        true,
    },
    {
      name:        "chunk-2",
      script:      "agent.py",
      interpreter: PYTHON,
      args: [
        "--key",           API_KEY,
        "--model",         MODEL,
        "--rpm",           RPM,
        "--input",         INPUT,
        "--offset",        "1255",
        "--limit",         "1255",
        "--output",        "output_chunk2.csv",
        "--progress-file", "progress_chunk2.json",
        "--resume",
      ],
      autorestart: false,
      log_file:    "logs/chunk2.log",
      error_file:  "logs/chunk2_err.log",
      time:        true,
    },
    {
      name:        "chunk-3",
      script:      "agent.py",
      interpreter: PYTHON,
      args: [
        "--key",           API_KEY,
        "--model",         MODEL,
        "--rpm",           RPM,
        "--input",         INPUT,
        "--offset",        "2510",
        "--limit",         "1256",
        "--output",        "output_chunk3.csv",
        "--progress-file", "progress_chunk3.json",
        "--resume",
      ],
      autorestart: false,
      log_file:    "logs/chunk3.log",
      error_file:  "logs/chunk3_err.log",
      time:        true,
    },
  ],
};
