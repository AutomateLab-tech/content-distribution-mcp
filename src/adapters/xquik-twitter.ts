import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";
import type { ChannelAdapter } from "./index.js";

const DEFAULT_XQUIK_BASE_URL = "https://xquik.com";
const XQUIK_TWEET_PATH = "/api/v1/x/tweets";
const X_POST_LIMIT = 280;
const REQUEST_TIMEOUT_MS = 30_000;

interface XquikPostResponse {
  data?: {
    id?: unknown;
    tweetId?: unknown;
    url?: unknown;
  };
  tweet?: {
    id?: unknown;
    url?: unknown;
  };
  id?: unknown;
  tweetId?: unknown;
  url?: unknown;
}

interface XquikConfig {
  account: string;
  apiKey: string;
  baseUrl: string;
}

function credential(profile: Profile, key: string): string {
  const profileValue = profile.credentials[key]?.trim();
  if (profileValue) return profileValue;
  return (process.env[key] ?? "").trim();
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export function getXquikConfig(profile: Profile, variant: Variant): XquikConfig {
  const apiKey = credential(profile, "XQUIK_API_KEY")
    || credential(profile, "HERMES_TWEET_API_KEY");
  const baseUrl = trimTrailingSlash(
    credential(profile, "XQUIK_BASE_URL") || DEFAULT_XQUIK_BASE_URL,
  );
  const accountFromChannel = variant.channel.split(":")[1] ?? "";
  const accountFromExtras = typeof variant.extras.account === "string"
    ? variant.extras.account
    : "";
  const account = accountFromExtras.trim()
    || credential(profile, "XQUIK_ACCOUNT")
    || credential(profile, "HERMES_TWEET_ACCOUNT")
    || accountFromChannel.trim();

  return { account, apiKey, baseUrl };
}

export function buildXquikUrl(baseUrl: string): string {
  return new URL(`${trimTrailingSlash(baseUrl)}${XQUIK_TWEET_PATH}`).toString();
}

export function buildXquikHeaders(apiKey: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "User-Agent": "content-distribution-mcp/xquik-twitter",
  };

  if (apiKey.startsWith("xq_")) {
    headers["x-api-key"] = apiKey;
  } else {
    headers.Authorization = `Bearer ${apiKey}`;
  }

  return headers;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function extractTweetId(payload: XquikPostResponse): string {
  return stringValue(payload.data?.id)
    || stringValue(payload.data?.tweetId)
    || stringValue(payload.tweet?.id)
    || stringValue(payload.id)
    || stringValue(payload.tweetId);
}

function extractTweetUrl(payload: XquikPostResponse, account: string): string | undefined {
  const explicitUrl = stringValue(payload.data?.url)
    || stringValue(payload.tweet?.url)
    || stringValue(payload.url);
  if (explicitUrl) return explicitUrl;

  const id = extractTweetId(payload);
  if (!id || !account) return undefined;

  return `https://x.com/${account.replace(/^@/, "")}/status/${id}`;
}

async function readJson(response: Response): Promise<XquikPostResponse> {
  const text = await response.text();
  if (!text) return {};

  try {
    return JSON.parse(text) as XquikPostResponse;
  } catch {
    return { data: { id: "" } };
  }
}

async function postWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function requestFailure(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return `Hermes Tweet publish timed out after ${REQUEST_TIMEOUT_MS / 1_000} seconds`;
  }
  if (error instanceof Error && error.message) {
    return `Hermes Tweet publish failed: ${error.message}`;
  }
  return "Hermes Tweet publish failed: network request failed";
}

export class XquikTwitterAdapter implements ChannelAdapter {
  constructor(private readonly fallback: ChannelAdapter) {}

  hints(): ChannelHints {
    return {
      max_length: X_POST_LIMIT,
      supported_md_features: ["links"],
      cta_placement: "none",
      canonical_url_supported: false,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const config = getXquikConfig(profile, variant);
    if (!config.apiKey) {
      return this.fallback.publish(variant, profile);
    }

    if (!config.account) {
      return {
        channel: variant.channel,
        state: "failed",
        error: "XQUIK_ACCOUNT or HERMES_TWEET_ACCOUNT required for automated Twitter/X publishing",
      };
    }

    let response: Response;
    try {
      response = await postWithTimeout(buildXquikUrl(config.baseUrl), {
        method: "POST",
        headers: buildXquikHeaders(config.apiKey),
        body: JSON.stringify({
          account: config.account,
          text: variant.body.slice(0, X_POST_LIMIT),
        }),
      });
    } catch (error) {
      return {
        channel: variant.channel,
        state: "failed",
        error: requestFailure(error),
      };
    }
    const payload = await readJson(response);

    if (!response.ok) {
      const detail = stringValue((payload as { error?: unknown }).error)
        || stringValue((payload as { message?: unknown }).message)
        || response.statusText
        || "request failed";
      return {
        channel: variant.channel,
        state: "failed",
        error: `Hermes Tweet publish failed (${response.status}): ${detail}`,
      };
    }

    return {
      channel: variant.channel,
      state: "live",
      live_url: extractTweetUrl(payload, config.account),
      published_at: new Date().toISOString(),
    };
  }

  async unpublish(liveUrl: string, profile: Profile): Promise<[boolean, string | undefined]> {
    return this.fallback.unpublish(liveUrl, profile);
  }
}
