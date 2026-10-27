import { Compass } from "lucide-react";
import { Link } from "react-router";

import { EmptyState } from "@/components/ui";

export default function NotFound() {
  return (
    <div className="p-6">
      <h1 className="sr-only">Page not found</h1>
      <EmptyState icon={Compass} title="Page not found" body="The page you are looking for does not exist." action={<Link to="/" className="text-accent underline">Go home</Link>} />
    </div>
  );
}
