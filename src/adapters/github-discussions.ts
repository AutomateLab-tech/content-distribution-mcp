import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

export class GitHubDiscussionsAdapter {
  hints(): ChannelHints {
    return {
      max_length: 65_000,
      supported_md_features: ["bold", "italic", "code_inline", "code_block", "links", "headers", "images", "lists", "tables", "blockquote"],
      cta_placement: "footer",
      canonical_url_supported: false,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const token = profile.credentials["GITHUB_TOKEN"];
    const repo = (variant.extras?.repo as string) || profile.credentials["GITHUB_DISCUSSION_REPO"];
    const categoryId = (variant.extras?.category_id as string) || profile.credentials["GITHUB_DISCUSSION_CATEGORY_ID"];

    if (!token || !repo) {
      return { channel: variant.channel, state: "failed", error: "GITHUB_TOKEN and GITHUB_DISCUSSION_REPO required in profile" };
    }

    const [owner, repoName] = repo.split("/");
    const repoRes = await this.gql(token, `query {
      repository(owner:"${owner}",name:"${repoName}") {
        id
        discussionCategories(first:20) { nodes { id name } }
      }
    }`);

    const repoData = repoRes.data?.repository as { id: string; discussionCategories: { nodes: Array<{ id: string; name: string }> } } | undefined;
    if (!repoData) return { channel: variant.channel, state: "failed", error: "GitHub repo not found or token lacks access" };

    const catId = categoryId
      || repoData.discussionCategories.nodes.find(c => c.name === (variant.extras?.category as string))?.id
      || repoData.discussionCategories.nodes.find(c => c.name === "General")?.id;
    if (!catId) return { channel: variant.channel, state: "failed", error: "GitHub Discussions category not found — set extras.category_id or extras.category" };

    const body = variant.canonical_url
      ? `${variant.body}\n\n---\n*Originally published at [${variant.canonical_url}](${variant.canonical_url})*`
      : variant.body;

    const mutation = `mutation {
      createDiscussion(input:{
        repositoryId:${JSON.stringify(repoData.id)},
        categoryId:${JSON.stringify(catId)},
        title:${JSON.stringify(variant.title)},
        body:${JSON.stringify(body)}
      }) { discussion { url } }
    }`;

    const result = await this.gql(token, mutation);
    const url = (result.data?.createDiscussion as { discussion?: { url: string } } | undefined)?.discussion?.url;
    if (!url) return { channel: variant.channel, state: "failed", error: JSON.stringify(result.errors ?? "no URL returned") };

    return { channel: variant.channel, state: "live", live_url: url, published_at: new Date().toISOString() };
  }

  async unpublish(_liveUrl: string, _profile: Profile): Promise<[boolean, string]> {
    return [false, "GitHub Discussions delete not yet implemented — use the GitHub UI or API directly"];
  }

  private async gql(token: string, query: string) {
    const res = await fetch("https://api.github.com/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `bearer ${token}` },
      body: JSON.stringify({ query }),
    });
    return res.json() as Promise<{ data?: Record<string, unknown>; errors?: unknown[] }>;
  }
}
