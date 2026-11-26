import { setRequestLocale } from "next-intl/server";
import { HistoryClient } from "@/components/report/history-client";

export default async function HistoryPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);
  return <HistoryClient />;
}
