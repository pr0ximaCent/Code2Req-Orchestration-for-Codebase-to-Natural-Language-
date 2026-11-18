package io.demo.graphql;

import io.demo.application.ArticleService;

@DgsComponent
public class ArticleMutation {
  private ArticleService articleService;

  @DgsMutation(field = MUTATION.CreateArticle)
  public DataFetcherResult<ArticlePayload> createArticle(@InputArgument("input") CreateArticleInput input) {
    Article article = articleService.createArticle(input.toParam()).orElseThrow(InvalidRequestException::new);
    return DataFetcherResult.<ArticlePayload>newResult().localContext(article).build();
  }

  @DgsData(parentType = MUTATION.TYPE_NAME, field = MUTATION.DeleteArticle)
  public DeletionStatus deleteArticle(@InputArgument("slug") String slug) {
    return DeletionStatus.newBuilder().success(true).build();
  }
}
