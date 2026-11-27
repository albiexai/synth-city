import type { TeamCampaignSummary } from "@/types/agentTeams";

/** #1415: a team's Progress card shows while its recorded campaign is active or finishing. A finished campaign has
 * no card. A team without a campaign record shows while work is under way, queued, not started or needs attention;
 * finished work alone never brings a card back, and "finished" is never inferred from the counts. */
export function campaignShown(campaign: TeamCampaignSummary | undefined) {
  if (!campaign) return false;
  if (campaign.campaign_status === "active" || campaign.campaign_status === "finishing") return true;
  if (campaign.campaign_status === "finished") return false;
  return campaign.under_way_count + (campaign.attention_count ?? 0) + campaign.queued_count +
    (campaign.not_started_count ?? 0) > 0 || campaign.decisions_ahead.length > 0 ||
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
