import { ArrowLeft } from "lucide-react";
import { CdnaiLogo } from "@/components/brand/CdnaiBrand";

type PrivacyPolicyProps = {
  onBack: () => void;
};

const SECTIONS: { title: string; body: React.ReactNode }[] = [
  {
    title: "1. Overview",
    body: (
      <>
        CDNAI ("we", "us") is an AI work partner that helps you write, build, analyze data and
        automate tasks. This Privacy Policy explains what information we collect, how we use it,
        and the choices you have. By creating an account or using CDNAI, you agree to the practices
        described here.
      </>
    ),
  },
  {
    title: "2. Information we collect",
    body: (
      <ul className="list-disc space-y-1.5 pl-5">
        <li><strong>Account information:</strong> your name, email address, and a securely hashed password.</li>
        <li><strong>Content you provide:</strong> the messages, prompts, files and uploads you send while using the assistant.</li>
        <li><strong>Usage data:</strong> basic information about how features are used (for example credits consumed and task outcomes) to operate and improve the service.</li>
        <li><strong>Technical data:</strong> device and connection details needed to keep the service secure and reliable.</li>
      </ul>
    ),
  },
  {
    title: "3. How we use your information",
    body: (
      <ul className="list-disc space-y-1.5 pl-5">
        <li>To provide, personalize and maintain the CDNAI services.</li>
        <li>To process your requests and generate the outputs you ask for.</li>
        <li>To keep your account secure and prevent abuse or fraudulent activity.</li>
        <li>To respond to support requests and communicate important service updates.</li>
        <li>To improve reliability, performance and product quality.</li>
      </ul>
    ),
  },
  {
    title: "4. AI processing",
    body: (
      <>
        When you interact with the assistant, your prompts and relevant context are processed by
        our underlying language-model and tooling providers in order to produce results. We do not
        sell your personal data. Where third-party models are used, they act as processors bound by
        confidentiality obligations.
      </>
    ),
  },
  {
    title: "5. Data storage & security",
    body: (
      <>
        Your data is stored in secure, access-controlled infrastructure. Credentials and secrets are
        encrypted at rest, and access follows the principle of least privilege. While no system can
        guarantee absolute security, we apply industry-standard safeguards to protect your information.
      </>
    ),
  },
  {
    title: "6. Data retention",
    body: (
      <>
        We retain account and content data only for as long as your account is active or as needed to
        provide the service and meet legal obligations. You may request deletion of your account and
        associated personal data at any time.
      </>
    ),
  },
  {
    title: "7. Sharing of information",
    body: (
      <>
        We share information only in limited circumstances: with service providers that help us operate
        CDNAI (under contract), when required by law or to protect rights and safety, and in connection
        with a business transfer such as a merger or acquisition. We do not sell your personal information.
      </>
    ),
  },
  {
    title: "8. Your rights & choices",
    body: (
      <>
        Depending on your location, you may have the right to access, correct, export or delete your
        personal data, and to object to certain processing. To exercise these rights, contact us using
        the details below. You can also manage your account settings within the product.
      </>
    ),
  },
  {
    title: "9. Children's privacy",
    body: (
      <>
        CDNAI is not directed to children under 13 (or the equivalent minimum age in your region). We do
        not knowingly collect personal data from children. If you believe a child has provided us data,
        please contact us so we can remove it.
      </>
    ),
  },
  {
    title: "10. Changes to this policy",
    body: (
      <>
        We may update this Privacy Policy from time to time. When we make material changes, we'll notify
        you through the product or other appropriate channels. The "Last updated" date above reflects the
        most recent revision.
      </>
    ),
  },
  {
    title: "11. Contact us",
    body: (
      <>
        Questions about this policy or your data? Reach out to our team through the support channel in
        the CDNAI workspace and we'll be glad to help.
      </>
    ),
  },
];

export function PrivacyPolicy({ onBack }: PrivacyPolicyProps) {
  return (
    <div className="min-h-full w-full bg-background text-foreground">
      <header className="sticky top-0 z-30 border-b border-border/60 bg-background/80 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-3xl items-center justify-between px-5 sm:px-8">
          <CdnaiLogo />
          <button
            type="button"
            onClick={onBack}
            className="inline-flex items-center gap-1.5 rounded-control px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" />
            Back
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-5 py-14 sm:px-8">
        <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">Privacy Policy</h1>
        <p className="mt-3 text-sm text-muted-foreground">Last updated: September 2026</p>
        <p className="mt-6 text-[15px] leading-relaxed text-muted-foreground">
          Your privacy matters. This policy describes how CDNAI collects, uses and protects your
          information in plain language.
        </p>

        <div className="mt-10 space-y-9">
          {SECTIONS.map((s) => (
            <section key={s.title}>
              <h2 className="text-lg font-semibold text-foreground">{s.title}</h2>
              <div className="mt-2 text-[15px] leading-relaxed text-muted-foreground [&_strong]:text-foreground">
                {s.body}
              </div>
            </section>
          ))}
        </div>

        <div className="mt-14 rounded-panel border border-border/70 bg-card p-6 text-sm text-muted-foreground">
          By continuing to use CDNAI you acknowledge that you have read and understood this Privacy
          Policy.
        </div>
      </main>

      <footer className="border-t border-border/60">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-5 py-6 text-sm text-muted-foreground sm:px-8">
          <span>© {new Date().getFullYear()} CDNAI</span>
          <button type="button" onClick={onBack} className="transition-colors hover:text-foreground">
            Return to home
          </button>
        </div>
      </footer>
    </div>
  );
}
