import {
  getLastResetDate,
  resetAllBalancesToDefault,
  updateLastResetDate,
} from "@/lib/db/users";

let schedulerInterval: ReturnType<typeof setInterval> | null = null;
let isInitialized = false;

// Get the configured reset day from environment (1-31), default is 1 (first of month)
export function getConfiguredResetDay(): number {
  const day = parseInt(process.env.BALANCE_RESET_DAY || "1", 10);
  return Math.min(Math.max(day, 1), 31);
}

// Check if auto-reset is enabled (BALANCE_RESET_DAY > 0)
export function isAutoResetEnabled(): boolean {
  const day = parseInt(process.env.BALANCE_RESET_DAY || "1", 10);
  return day > 0;
}

// Effective reset day for the given month (handles months shorter than configured day)
export function getEffectiveResetDay(date = new Date()): number {
  const configured = getConfiguredResetDay();
  const lastDayOfMonth = new Date(
    date.getFullYear(),
    date.getMonth() + 1,
    0
  ).getDate();

  return Math.min(configured, lastDayOfMonth);
}

// Check if we should reset now (also catches up if the reset day was missed)
export async function shouldAutoResetNow(date = new Date()): Promise<boolean> {
  if (!isAutoResetEnabled()) {
    return false;
  }

  const resetDay = getEffectiveResetDay(date);
  const currentDay = date.getDate();

  // Don't reset before the configured day for this month
  if (currentDay < resetDay) {
    return false;
  }

  // Check if we already reset this month
  const lastReset = await getLastResetDate();
  if (lastReset) {
    const lastResetMonth = lastReset.getMonth();
    const lastResetYear = lastReset.getFullYear();
    const currentMonth = date.getMonth();
    const currentYear = date.getFullYear();

    // Already reset this month
    if (lastResetMonth === currentMonth && lastResetYear === currentYear) {
      return false;
    }
  }

  return true;
}

// Perform the automatic reset check
async function checkAndReset(): Promise<void> {
  try {
    const now = new Date();
    const shouldReset = await shouldAutoResetNow(now);
    
    if (shouldReset) {
      const configured = getConfiguredResetDay();
      const effective = getEffectiveResetDay(now);
      const currentDay = now.getDate();
      const note = currentDay === effective ? "reset day" : "catch-up";
      console.log(
        `[Scheduler] ${note} triggered (configured=${configured}, effective=${effective}, today=${currentDay}). Resetting all balances to default...`
      );
      const count = await resetAllBalancesToDefault();
      await updateLastResetDate();
      console.log(`[Scheduler] Successfully reset balances for ${count} users.`);
    }
  } catch (error) {
    console.error("[Scheduler] Error during auto-reset check:", error);
  }
}

// Initialize the scheduler
export function initScheduler(): void {
  if (isInitialized) {
    console.log("[Scheduler] Already initialized, skipping...");
    return;
  }

  if (!isAutoResetEnabled()) {
    console.log("[Scheduler] Auto-reset disabled (BALANCE_RESET_DAY=0 or not set)");
    return;
  }

  const configured = getConfiguredResetDay();
  const effective = getEffectiveResetDay(new Date());
  console.log(
    `[Scheduler] Initializing (configured reset day: ${configured}, effective this month: ${effective})`
  );

  // Check immediately on startup
  checkAndReset();

  // Check every hour (3600000ms)
  schedulerInterval = setInterval(checkAndReset, 60 * 60 * 1000);

  isInitialized = true;
  console.log("[Scheduler] Started - checking every hour for reset day");
}

// Stop the scheduler
export function stopScheduler(): void {
  if (schedulerInterval) {
    clearInterval(schedulerInterval);
    schedulerInterval = null;
    isInitialized = false;
    console.log("[Scheduler] Stopped");
  }
}

// Get scheduler status
export function getSchedulerStatus(): {
  enabled: boolean;
  resetDay: number;
  effectiveResetDay: number;
  isRunning: boolean;
} {
  return {
    enabled: isAutoResetEnabled(),
    resetDay: getConfiguredResetDay(),
    effectiveResetDay: getEffectiveResetDay(new Date()),
    isRunning: isInitialized,
  };
}
