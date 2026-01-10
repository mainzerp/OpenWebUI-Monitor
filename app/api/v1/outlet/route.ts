import { NextResponse } from "next/server";
import { encode } from "gpt-tokenizer/model/gpt-4";
import { Pool, PoolClient } from "pg";
import { createClient } from "@vercel/postgres";
import { query, getClient } from "@/lib/db/client";
import { getModelInletCost } from "@/lib/utils/inlet-cost";
import { ensureRuntimeInitialized } from "@/lib/runtime";

const isVercel = process.env.VERCEL === "1";

interface Message {
  role: string;
  content: string;
}

interface ModelPrice {
  id: string;
  name: string;
  input_price: number;
  output_price: number;
  per_msg_price: number;
}

type DbClient = ReturnType<typeof createClient> | Pool | PoolClient;

async function getModelPrice(modelId: string): Promise<ModelPrice | null> {
  const result = await query(
    `SELECT id, name, input_price, output_price, per_msg_price 
     FROM model_prices 
     WHERE id = $1`,
    [modelId]
  );

  if (result.rows[0]) {
    return result.rows[0];
  }

  const defaultInputPrice = parseFloat(
    process.env.DEFAULT_MODEL_INPUT_PRICE || "60"
  );
  const defaultOutputPrice = parseFloat(
    process.env.DEFAULT_MODEL_OUTPUT_PRICE || "60"
  );

  if (
    isNaN(defaultInputPrice) ||
    defaultInputPrice < 0 ||
    isNaN(defaultOutputPrice) ||
    defaultOutputPrice < 0
  ) {
    return null;
  }

  return {
    id: modelId,
    name: modelId,
    input_price: defaultInputPrice,
    output_price: defaultOutputPrice,
    per_msg_price: -1,
  };
}

export async function POST(req: Request) {
  const client = (await getClient()) as DbClient;
  let pgClient: DbClient | null = null;

  try {
    ensureRuntimeInitialized();
    if (isVercel) {
      pgClient = client;
    } else {
      pgClient = await (client as Pool).connect();
    }

    const data = await req.json();
    const modelId = data.body.model;
    const userId = data.user.id;
    const userName = data.user.name || "Unknown User";

    await query("BEGIN");

    const modelPrice = await getModelPrice(modelId);
    if (!modelPrice) {
      throw new Error(`Fail to fetch price info of model ${modelId}`);
    }

    const lastMessage = data.body.messages[data.body.messages.length - 1];

    let inputTokens: number;
    let outputTokens: number;
    let tokenSource: string;
    
    if (
      lastMessage.usage &&
      lastMessage.usage.prompt_tokens &&
      lastMessage.usage.completion_tokens
    ) {
      inputTokens = lastMessage.usage.prompt_tokens;
      outputTokens = lastMessage.usage.completion_tokens;
      tokenSource = "LLM_USAGE";
      console.log(`[Outlet] Token source: LLM usage data (prompt: ${inputTokens}, completion: ${outputTokens})`);
    } else {
      console.log(`[Outlet] No usage data from LLM, falling back to tokenization...`);
      console.log(`[Outlet] lastMessage.usage:`, JSON.stringify(lastMessage.usage || null));
      
      const startTime = Date.now();
      const totalChars = data.body.messages.reduce((sum: number, msg: Message) => sum + (msg.content?.length || 0), 0);
      const outputChars = lastMessage.content?.length || 0;
      console.log(`[Outlet] Total chars to tokenize: ${totalChars}`);
      
      // For large inputs (>50k chars), use estimation instead of tokenization
      // Tokenization can take minutes for very large texts and cause timeouts
      const CHAR_LIMIT_FOR_ESTIMATION = 50000;
      const CHARS_PER_TOKEN = 4; // Approximate ratio for English/code
      
      if (totalChars > CHAR_LIMIT_FOR_ESTIMATION) {
        // Use fast estimation: ~4 characters per token
        const totalTokens = Math.ceil(totalChars / CHARS_PER_TOKEN);
        outputTokens = Math.ceil(outputChars / CHARS_PER_TOKEN);
        inputTokens = totalTokens - outputTokens;
        tokenSource = "ESTIMATION";
        console.log(`[Outlet] Large input detected (${totalChars} chars), using estimation: ${totalTokens} tokens`);
      } else {
        outputTokens = encode(lastMessage.content).length;
        const totalTokens = data.body.messages.reduce(
          (sum: number, msg: Message) => sum + encode(msg.content).length,
          0
        );
        inputTokens = totalTokens - outputTokens;
        tokenSource = "TOKENIZER";
        
        const elapsed = Date.now() - startTime;
        console.log(`[Outlet] Tokenization completed in ${elapsed}ms (${totalChars} chars -> ${totalTokens} tokens)`);
      }
    }

    let totalCost: number;
    if (outputTokens === 0) {
      totalCost = 0;
      console.log("No charge for zero output tokens");
    } else if (modelPrice.per_msg_price >= 0) {
      totalCost = Number(modelPrice.per_msg_price);
      console.log(
        `Using fixed pricing: ${totalCost} (${modelPrice.per_msg_price} per message)`
      );
    } else {
      const inputCost = (inputTokens / 1_000_000) * modelPrice.input_price;
      const outputCost = (outputTokens / 1_000_000) * modelPrice.output_price;
      totalCost = inputCost + outputCost;
    }

    const inletCost = getModelInletCost(modelId);

    const actualCost = totalCost - inletCost;

    const userResult = await query(
      `UPDATE users 
       SET balance = LEAST(
         balance - CAST($1 AS DECIMAL(16,4)),
         999999.9999
       )
       WHERE id = $2
       RETURNING balance`,
      [actualCost, userId]
    );

    if (userResult.rows.length === 0) {
      throw new Error("User does not exist");
    }

    const newBalance = Number(userResult.rows[0].balance);

    if (newBalance > 999999.9999) {
      throw new Error("Balance exceeds maximum allowed value");
    }

    await query(
      `INSERT INTO user_usage_records (
        user_id, nickname, model_name, 
        input_tokens, output_tokens, 
        cost, balance_after
      ) VALUES ($1, $2, $3, $4, $5, $6, $7)`,
      [
        userId,
        userName,
        modelId,
        inputTokens,
        outputTokens,
        totalCost,
        newBalance,
      ]
    );

    await query("COMMIT");

    console.log(
      JSON.stringify({
        success: true,
        inputTokens,
        outputTokens,
        totalCost,
        newBalance,
        message: "Request successful",
      })
    );

    return NextResponse.json({
      success: true,
      inputTokens,
      outputTokens,
      totalCost,
      newBalance,
      message: "Request successful",
    });
  } catch (error) {
    await query("ROLLBACK");
    console.error("Outlet error:", error);
    return NextResponse.json(
      {
        success: false,
        error:
          error instanceof Error ? error.message : "Error processing request",
        error_type: error instanceof Error ? error.name : "UNKNOWN_ERROR",
      },
      { status: 500 }
    );
  } finally {
    if (!isVercel && pgClient && "release" in pgClient) {
      pgClient.release();
    }
  }
}
