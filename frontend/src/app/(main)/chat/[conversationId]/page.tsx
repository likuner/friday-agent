import ChatWorkspace from '@/components/ChatWorkspace';
export default async function ConversationPage({ params }: { params: Promise<{ conversationId: string }> }) {
  const { conversationId } = await params;
  return <ChatWorkspace conversationId={conversationId} />;
}
