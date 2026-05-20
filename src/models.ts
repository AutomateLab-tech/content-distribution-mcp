export interface Content {
  id: string;
  title: string;
  subtitle?: string;
  body_md: string;
  cover_image?: string;
  tags: string[];
  canonical_url?: string;
  cta_block?: string;
  author: string;
  source_task_id?: string;
}

export interface Variant {
  channel: string;
  title: string;
  body: string;
  tags: string[];
  canonical_url?: string;
  cta_block?: string;
  schedule_at?: string;
  extras: Record<string, unknown>;
}

export type PublishState = "live" | "queued" | "needs_browser" | "failed";

export interface PublishResult {
  channel: string;
  state: PublishState;
  live_url?: string;
  draft_path?: string;
  compose_url?: string;
  error?: string;
  published_at?: string;
}

export interface ChannelHints {
  max_length?: number;
  supported_md_features: string[];
  tag_vocab?: string[];
  cta_placement: "top" | "bottom" | "footer" | "none";
  canonical_url_supported: boolean;
  browser_only: boolean;
}
