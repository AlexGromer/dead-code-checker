using Xunit;
public class ServiceTests
{
    [Fact]
    public void RunWorks() { new Service(new MemRepo()).Run(); }
}
