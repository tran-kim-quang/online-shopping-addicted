import { setRequestLocale } from "next-intl/server";
import { SourcePageClient } from "@/components/report/source-page-client";

export default async function SourceDetailPage({
  params,
}: {
  params: Promise<{ locale: string; id: string }>;
}) {
  const { locale, id } = await params;
  setRequestLocale(locale);
  return <SourcePageClient sourceId={id} />;
}
