import { ArrowLeft, Compass, Home } from "lucide-react";
import { useNavigate } from "react-router";

import { Button, ButtonLink, Card, EmptyState, Page } from "@/components/ui";

/** 404: the empty state's title is the page's single h1, inside the same Card frame as every other empty state (C-23). */
export default function NotFound() {
  const navigate = useNavigate();
  return (
    <Page>
      <Card as="div">
        <EmptyState icon={Compass} titleAs="h1" title="Page not found" body="The page you are looking for does not exist or has moved."
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <ButtonLink to="/" variant="primary" icon={Home}>Go home</ButtonLink>
              <Button icon={ArrowLeft} onClick={() => navigate(-1)}>Back</Button>
            </div>
          } />
      </Card>
    </Page>
  );
}
