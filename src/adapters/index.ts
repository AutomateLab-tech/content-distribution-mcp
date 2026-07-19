import { DevToAdapter } from "./devto.js";
import { HashnodeAdapter } from "./hashnode.js";
import { GitHubDiscussionsAdapter } from "./github-discussions.js";
import { RedditAdapter } from "./reddit.js";
import { BlueskyAdapter } from "./bluesky.js";
import { makeBrowserAdapter } from "./browser.js";
import { XquikTwitterAdapter } from "./xquik-twitter.js";
import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

export interface ChannelAdapter {
  hints(): ChannelHints;
  publish(variant: Variant, profile: Profile): Promise<PublishResult>;
  unpublish(liveUrl: string, profile: Profile): Promise<[boolean, string | undefined]>;
}

export function buildAdapterMap(): Record<string, ChannelAdapter> {
  const medium = makeBrowserAdapter("medium");
  const linkedin = makeBrowserAdapter("linkedin");
  const twitter = new XquikTwitterAdapter(makeBrowserAdapter("twitter"));

  return {
    devto: new DevToAdapter(),
    hashnode: new HashnodeAdapter(),
    github_discussions: new GitHubDiscussionsAdapter(),
    "github-discussions": new GitHubDiscussionsAdapter(),
    reddit: new RedditAdapter(),
    bluesky: new BlueskyAdapter(),
    medium,
    medium_browser: medium,
    "medium-browser": medium,
    linkedin,
    linkedin_browser: linkedin,
    "linkedin-browser": linkedin,
    twitter,
    twitter_browser: twitter,
    "twitter-browser": twitter,
    x: twitter,
  };
}
