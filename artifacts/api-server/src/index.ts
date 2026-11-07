import app from "./app";
import { logger } from "./lib/logger";
import { startTelegramBotWorker } from "./lib/telegram-bot-worker";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

const stopTelegramBot = startTelegramBotWorker();
const server = app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    stopTelegramBot();
    process.exit(1);
  }

  logger.info({ port }, "Server listening");
});

let isShuttingDown = false;
const shutdown = (signal: NodeJS.Signals): void => {
  if (isShuttingDown) return;
  isShuttingDown = true;
  logger.info({ signal }, "Shutting down");
  stopTelegramBot();

  const forceExit = setTimeout(() => {
    logger.error("Shutdown timed out; forcing process exit.");
    process.exit(1);
  }, 10_000);
  forceExit.unref();

  server.close((err) => {
    clearTimeout(forceExit);
    if (err) {
      logger.error({ err }, "Error while closing the server");
      process.exit(1);
    }
    process.exit(0);
  });
};

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
