import type { TeamCampaignSummary } from "@/types/agentTeams";

/** #1415: a team's Progress card shows while its recorded campaign is active or finishing. A finished campaign has
 * no card. A team without a campaign record shows only while work is under way, queued or needs attention: done
 * work, backlog or planned decisions alone never bring a card back, and "finished" is never inferred from counts. */
export function campaignShown(campaign: TeamCampaignSummary | undefined) {
  if (!campaign) return false;
  if (campaign.campaign_status === "active" || campaign.campaign_status === "finishing") return true;
  if (campaign.campaign_status === "finished") return false;
  // The observed dispatch order is queued work too.
  return campaign.under_way_count + campaign.queued_count + (campaign.attention_count ?? 0) > 0 ||
    (campaign.dispatch_order?.length ?? 0) > 0;
}

export function campaignFinished(campaign: TeamCampaignSummary | undefined) {
  return campaign?.campaign_status === "finished";
}

export function campaignBarLabel(c: TeamCampaignSummary) {
  const known = c.backlog_state === "available";
  const todo = c.queued_count + (c.not_started_count ?? 0);
  const attention = c.attention_count ?? 0;
  const needs = attention > 0 ? `${attention} need attention, ` : "";
  return `${c.done_count} done, ${c.under_way_count} under way, ${needs}${todo} still to do, backlog ${known ? "known" : "unknown"}`;
}
