import { Construction } from "lucide-react";

import { EmptyState, PageHeader } from "@/components/ui";

// Placeholder: implemented by the page owner (see docs/design/ui-spec.md).
export default function CouncilBuilder() {
  return (
    <div className="p-6 max-w-6xl mx-auto">
      <PageHeader title="CouncilBuilder" />
      <EmptyState icon={Construction} title="Coming soon" />
    </div>
  );
}
