import { NextResponse } from "next/server";
import { verifyApiToken } from "@/lib/auth";
import { resetAllBalancesToDefault, resetUserBalanceToDefault, getLastResetDate, updateLastResetDate } from "@/lib/db/users";
import { getConfiguredResetDay, getEffectiveResetDay, getSchedulerStatus, shouldAutoResetNow } from "@/lib/scheduler";
import { ensureRuntimeInitialized } from "@/lib/runtime";

// GET: Check reset status and configuration
export async function GET(req: Request) {
  const authError = verifyApiToken(req);
  if (authError) {
    return authError;
  }

  try {
    ensureRuntimeInitialized();
    const resetDay = getConfiguredResetDay();
    const effectiveResetDay = getEffectiveResetDay(new Date());
    const lastReset = await getLastResetDate();
    const shouldReset = await shouldAutoResetNow(new Date());
    const schedulerStatus = getSchedulerStatus();
    
    return NextResponse.json({
      success: true,
      reset_day: resetDay,
      effective_reset_day: effectiveResetDay,
      last_reset: lastReset?.toISOString() || null,
      should_reset_today: shouldReset,
      scheduler: schedulerStatus,
    });
  } catch (error) {
    console.error("Error checking reset status:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Failed to check reset status" },
      { status: 500 }
    );
  }
}

// Reset all users' balances to their default_balance
export async function POST(req: Request) {
  const authError = verifyApiToken(req);
  if (authError) {
    return authError;
  }

  try {
    ensureRuntimeInitialized();
    const body = await req.json().catch(() => ({}));
    const { userId, force } = body;

    if (userId) {
      // Reset single user
      const newBalance = await resetUserBalanceToDefault(userId);
      return NextResponse.json({
        success: true,
        message: `Balance reset for user ${userId}`,
        new_balance: newBalance,
      });
    } else {
      // Check if auto-reset should happen (unless forced)
      if (!force) {
        const shouldReset = await shouldAutoResetNow(new Date());
        if (!shouldReset) {
          const resetDay = getConfiguredResetDay();
          const effectiveResetDay = getEffectiveResetDay(new Date());
          const lastReset = await getLastResetDate();
          return NextResponse.json({
            success: false,
            message: `Reset not needed. Reset day is ${resetDay} (effective this month: ${effectiveResetDay}), last reset was ${lastReset?.toISOString() || 'never'}`,
            reset_day: resetDay,
            effective_reset_day: effectiveResetDay,
            last_reset: lastReset?.toISOString() || null,
          });
        }
      }
      
      // Reset all users
      const count = await resetAllBalancesToDefault();
      await updateLastResetDate();
      
      return NextResponse.json({
        success: true,
        message: `Reset balances for ${count} users to their default values`,
        users_affected: count,
        reset_date: new Date().toISOString(),
      });
    }
  } catch (error) {
    console.error("Error resetting balances:", error);
    return NextResponse.json(
      {
        error: error instanceof Error ? error.message : "Failed to reset balances",
      },
      { status: 500 }
    );
  }
}
