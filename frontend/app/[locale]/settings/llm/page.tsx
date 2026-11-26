import { setRequestLocale } from "next-intl/server";
import { LlmSettingsClient } from "@/components/llm/llm-settings-client";

export default async function LlmSettingsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);
  return <LlmSettingsClient />;
}
