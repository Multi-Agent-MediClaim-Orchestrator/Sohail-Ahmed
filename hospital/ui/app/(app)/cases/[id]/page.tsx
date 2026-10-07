import { redirect } from "next/navigation";
export default function CaseIndex({ params }: { params: { id: string } }) { redirect(`/cases/${params.id}/documents`); }
