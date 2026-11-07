import { spawn, type ChildProcess } from "node:child_process";
import { logger } from "./logger";

const RESTART_DELAY_MS = 5_000;
const BOT_ARGS = ["run", "--frozen", "python", "main.py"];

export function startTelegramBotWorker(): () => void {
  if (process.env.NODE_ENV !== "production") {
    return () => {};
  }

  if (!process.env.TELEGRAM_TOKEN) {
    throw new Error("TELEGRAM_TOKEN is required to start the production bot.");
  }

  let child: ChildProcess | undefined;
  let restartTimeout: NodeJS.Timeout | undefined;
  let restartScheduled = false;
  let isStopping = false;

  const start = (): void => {
    if (isStopping) return;

    const worker = spawn("uv", BOT_ARGS, {
      cwd: process.cwd(),
      env: process.env,
      stdio: "inherit",
    });
    child = worker;

    worker.once("error", (err) => {
      logger.error({ err }, "Unable to start the Telegram bot worker.");
      scheduleRestart();
    });
    worker.once("exit", (code, signal) => {
      if (child === worker) child = undefined;
      if (isStopping) return;
      logger.error({ code, signal }, "Telegram bot worker exited.");
      scheduleRestart();
    });
  };

  const scheduleRestart = (): void => {
    if (isStopping || restartScheduled) return;
    restartScheduled = true;
    restartTimeout = setTimeout(() => {
      restartScheduled = false;
      start();
    }, RESTART_DELAY_MS);
    restartTimeout.unref();
  };

  start();

  return () => {
    isStopping = true;
    if (restartTimeout) clearTimeout(restartTimeout);
    child?.kill("SIGTERM");
  };
}
