import { setRequestLocale } from "next-intl/server";
import { DataProvidersClient } from "@/components/data-providers/data-providers-client";

export default async function DataSourcesSettingsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);
  return <DataProvidersClient />;
}
