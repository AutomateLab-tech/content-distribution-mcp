import { DevToAdapter } from "./devto.js";
import { HashnodeAdapter } from "./hashnode.js";
import { GitHubDiscussionsAdapter } from "./github-discussions.js";
import { RedditAdapter } from "./reddit.js";
import { BlueskyAdapter } from "./bluesky.js";
import { GetXAPITwitterAdapter } from "./getxapi-twitter.js";
import { makeBrowserAdapter } from "./browser.js";
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
  const twitter = makeBrowserAdapter("twitter");

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
    twitter_getxapi: new GetXAPITwitterAdapter(),
    "twitter-getxapi": new GetXAPITwitterAdapter(),
    getxapi_twitter: new GetXAPITwitterAdapter(),
    "getxapi-twitter": new GetXAPITwitterAdapter(),
  };
}
