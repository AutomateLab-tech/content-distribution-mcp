export interface SubredditRule {
  subreddit: string;
  posting_cooldown_days: number;
  self_promo_ratio_max: number;
  flair_vocab: string[];
  last_posted_at?: string;
  account_age_min_days: number;
  karma_min: number;
  notes?: string;
}

export interface PostLogEntry {
  content_id: string;
  channel: string;
  state: string;
  published_url?: string;
  error?: string;
  updated_at?: string;
  retry_count?: number;
  next_retry_at?: string;
}

export interface ProfileChannel {
  channel: string;
}

export interface Profile {
  name: string;
  credentials: Record<string, string>;
  channels?: ProfileChannel[];
  subreddits?: string[];
}

export interface ScheduledItem {
  id: string;
  content_id: string;
  channel: string;
  variant: unknown;
  schedule_at: string;
}

export interface StateBackend {
  loadProfile(name: string): Profile;
  listProfiles(): string[];
  markPublished(entry: PostLogEntry): void;
  listPostLog(opts?: { content_id?: string; channel?: string }): PostLogEntry[];
  getPostLog(contentId: string, channel: string): PostLogEntry | null;
  enqueueScheduled(contentId: string, channel: string, variant: unknown, scheduledAt: string): string;
  listScheduled(before?: string): ScheduledItem[];
  dequeueScheduled(id: string): void;
  loadSubredditRules(subreddit: string): SubredditRule | null;
  listSubreddits(): SubredditRule[];
  updateSubredditLastPosted(subreddit: string, postedAt: string): void;
}
